"""Health check for the live pipeline. Runs every five minutes from launchd.

Writes one JSON line per run to data/health.jsonl. When something is wrong it appends a
line to data/alerts.log and shows a macOS notification, at most once per problem per hour
(state in data/healthcheck_state.json). Run by hand: python scripts/healthcheck.py
"""
import json, os, re, shutil, subprocess, sys, time
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from dotenv import load_dotenv; load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
import psycopg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
JOBS = ["com.tethera.follow-live", "com.tethera.follow-uniswapx", "com.tethera.follow-uniswapx-orders", "com.tethera.api"]
MAX_LAG_BLOCKS, MAX_LAG_SECONDS, MIN_DISK_GB, GAP_BLOCKS = 100, 60, 10, 100
NOTIFY_EVERY = 3600


def pg(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


def launchd_jobs():
    out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    jobs = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2].startswith("com.tethera."):
            jobs[parts[2]] = int(parts[0]) if parts[0].isdigit() else None
    return jobs


def main():
    now = datetime.now(timezone.utc)
    h = {"at": now.isoformat(timespec="seconds"), "ok": True, "problems": []}
    problems = h["problems"]

    # (f) Postgres, (a) lag, (c) UniswapX tables, (d) gaps
    try:
        with psycopg.connect(DB, connect_timeout=10) as conn:
            conn.execute("SET statement_timeout = '60s'")
            latest, = pg(conn, "SELECT max(block_time) FROM swaps WHERE block_time > now() - interval '1 day'")
            latest_block, = pg(conn, "SELECT max(block_number) FROM swaps WHERE block_time > now() - interval '1 day'")
            h["latest_swap"] = latest.isoformat(timespec="seconds") if latest else None
            h["latest_block"] = latest_block
            h["lag_seconds"] = round((now - latest).total_seconds()) if latest else None
            fills, = pg(conn, "SELECT count(*) FROM uniswapx_fills WHERE block_time > now() - interval '30 minutes'")
            orders, = pg(conn, "SELECT count(*) FROM uniswapx_orders WHERE first_seen > now() - interval '30 minutes'")
            h["uniswapx_fills_30m"], h["uniswapx_orders_30m"] = fills, orders
            gaps = conn.execute("""
                WITH b AS (SELECT DISTINCT block_number FROM swaps WHERE block_time > now() - interval '6 hours'),
                     g AS (SELECT block_number, lead(block_number) OVER (ORDER BY block_number) AS nxt FROM b)
                SELECT block_number + 1, nxt - 1 FROM g WHERE nxt - block_number > %s ORDER BY 1""", (GAP_BLOCKS,)).fetchall()
            h["gaps"] = [[int(a), int(b)] for a, b in gaps]
    except Exception as e:  # noqa: BLE001
        h["postgres_error"] = f"{type(e).__name__}: {str(e)[:120]}"
        problems.append(f"postgres: {h['postgres_error']}")

    if h.get("lag_seconds") is None and "postgres_error" not in h:
        problems.append("no swaps in the last day")
    elif h.get("lag_seconds") is not None and h["lag_seconds"] > MAX_LAG_SECONDS:
        problems.append(f"swaps lag {h['lag_seconds']}s")
    if h.get("gaps"):
        problems.append(f"{len(h['gaps'])} gap(s) in swaps, first {h['gaps'][0][0]}-{h['gaps'][0][1]}")
    if "postgres_error" not in h:
        if not h["uniswapx_fills_30m"]:
            problems.append("no uniswapx fills in 30 min")
        if not h["uniswapx_orders_30m"]:
            problems.append("no uniswapx orders in 30 min")

    # chain head via the failover client
    try:
        from tethera.rpc import live_rpc
        rpc = live_rpc(retries=2)
        head = rpc.block_number()
        h["head"] = head
        h["rpc_endpoint"] = rpc.active.split("/")[2]
        if h.get("latest_block"):
            h["lag_blocks"] = head - h["latest_block"]
            if h["lag_blocks"] > MAX_LAG_BLOCKS:
                problems.append(f"swaps {h['lag_blocks']} blocks behind head")
    except Exception as e:  # noqa: BLE001
        h["rpc_error"] = f"{type(e).__name__}: {str(e)[:120]}"
        problems.append(f"rpc: {h['rpc_error']}")

    # (b) launchd jobs
    jobs = launchd_jobs()
    h["jobs"] = {j: jobs.get(j) for j in JOBS}
    for j in JOBS:
        if j not in jobs:
            problems.append(f"{j} not loaded")
        elif jobs[j] is None:
            problems.append(f"{j} not running")

    # (e) disk
    h["disk_free_gb"] = {}
    for path in ("/", "/path/to/archive"):
        try:
            free = shutil.disk_usage(path).free / 1e9
            h["disk_free_gb"][path] = round(free, 1)
            if free < MIN_DISK_GB:
                problems.append(f"{path} has {free:.1f} GB free")
        except OSError:
            h["disk_free_gb"][path] = None
            problems.append(f"{path} not mounted")

    h["ok"] = not problems
    with open(os.path.join(DATA, "health.jsonl"), "a") as f:
        f.write(json.dumps(h) + "\n")

    if problems:
        state_path = os.path.join(DATA, "healthcheck_state.json")
        try:
            state = json.load(open(state_path))
        except (OSError, ValueError):
            state = {}
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(DATA, "alerts.log"), "a") as f:
            for p in problems:
                f.write(f"{stamp} {p}\n")
        fresh = []
        for p in problems:
            key = re.sub(r"\d+", "#", p)   # problem class, without the changing numbers
            if time.time() - state.get(key, 0) > NOTIFY_EVERY:
                fresh.append(p); state[key] = time.time()
        if fresh:
            msg = "; ".join(fresh)[:200].replace('"', "'")
            subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "Tethera"'], capture_output=True)
        json.dump(state, open(state_path, "w"))
    print(json.dumps(h))
    return 0


if __name__ == "__main__":
    sys.exit(main())
