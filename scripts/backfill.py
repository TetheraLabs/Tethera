"""Resumable, parallel historical backfill of Uniswap swaps into `swaps_archive` (the cold table on
the external drive). The hot `swaps` table on the server only holds the live tail.

Chunks of 2,000 blocks are handed out newest-first, from just below the oldest live block down to
--to (default: block 653325, the first block of 2026-07-01 UTC). Each finished chunk is recorded in
`backfill_chunks` inside the same transaction as its rows, so workers may finish out of order and a
restart only re-queues chunks that never completed. Chunks the earlier sequential run finished
(contiguous range already present in swaps_archive) are recognised and skipped.

  --workers N   parallel fetchers (default 8 with ARCHIVE_RPC_URL set, else 1)
  --rps R       total request budget per second across workers (default 200 with ARCHIVE_RPC_URL,
                else 2 for the public endpoint). Paid endpoints bill batch sub-requests individually,
                so they count against the budget; the public endpoint throttles per HTTP request.
  --to BLOCK    stop block (inclusive)
  --max-chunks  stop after this many chunks (for testing)

Set ARCHIVE_RPC_URL in .env to use a paid archive endpoint; otherwise the public RPC is used.
"""
import os, sys, time, threading, queue
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import psycopg
from dotenv import load_dotenv
from tethera.rpc import Rpc, RateLimiter
from tethera import swaps as sw

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

def arg(name, default, cast=str):
    return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default

ARCHIVE_RPC = os.environ.get("ARCHIVE_RPC_URL") or None
TO = arg("--to", 653_325, int)
WORKERS = arg("--workers", 8 if ARCHIVE_RPC else 1, int)
RPS = arg("--rps", 200.0 if ARCHIVE_RPC else 2.0, float)
MAX_CHUNKS = arg("--max-chunks", None, int)
CH = 2_000
DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)
limiter = RateLimiter(RPS, per_item=bool(ARCHIVE_RPC))

# -- work list -----------------------------------------------------------------------------------
with psycopg.connect(DB) as conn, conn.cursor() as cur:
    cur.execute("SELECT min(block_number) FROM swaps"); top = cur.fetchone()[0] - 1
    cur.execute("SELECT min(block_number), max(block_number) FROM swaps_archive"); amin, amax = cur.fetchone()
    cur.execute("SELECT lo FROM backfill_chunks"); done = {r[0] for r in cur.fetchall()}
    todo = []
    hi = top
    while hi >= TO:
        lo = max(hi - CH + 1, TO)
        # the sequential run wrote contiguously from `top` down to amin; treat that range as finished
        seq_done = amin is not None and amax is not None and lo >= amin and hi <= amax + CH
        if lo not in done and not seq_done:
            todo.append((lo, hi))
        hi = lo - 1
    if amin is not None:
        cur.executemany("INSERT INTO backfill_chunks (lo, hi, swaps) VALUES (%s, %s, -1) ON CONFLICT DO NOTHING",
                        [(lo, hi) for lo, hi in ((max(h - CH + 1, TO), h) for h in range(top, amin - 1, -CH)) if lo >= amin])
        conn.commit()
if MAX_CHUNKS:
    todo = todo[:MAX_CHUNKS]
total_blocks = sum(hi - lo + 1 for lo, hi in todo)
log(f"{len(todo):,} chunks / {total_blocks:,} blocks to fetch, {WORKERS} workers, {RPS:g} req/s, "
    f"{'archive rpc' if ARCHIVE_RPC else 'public rpc'} -> swaps_archive")

q: "queue.Queue[tuple[int, int]]" = queue.Queue()
for c in todo:
    q.put(c)
stats = {"chunks": 0, "blocks": 0, "swaps": 0, "errors": 0}
lock = threading.Lock()
t0 = time.time()
stop = threading.Event()

INSERT = """INSERT INTO swaps_archive (protocol, pool, block_number, block_time, tx_hash, log_index, event_sender,
            recipient, tx_from, amount0, amount1, direction)
            VALUES (%s,%s,%s,to_timestamp(%s),%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (tx_hash, log_index) DO NOTHING"""

def worker(i: int):
    rpc = Rpc(url=ARCHIVE_RPC, retries=60, limiter=limiter)
    with psycopg.connect(DB) as conn, conn.cursor() as cur:
        while not stop.is_set():
            try:
                lo, hi = q.get_nowait()
            except queue.Empty:
                return
            t = time.time()
            for attempt in range(1000):
                try:
                    chunk = sw.fetch_swaps(rpc, lo, hi, chunk=CH)
                    sw.attach_tx_from(rpc, chunk, batch_size=25)
                    break
                except Exception as e:
                    with lock:
                        stats["errors"] += 1
                    log(f"w{i} blocks {lo}-{hi}: {type(e).__name__}: {str(e)[:80]}; retry in 30s")
                    time.sleep(30)
            rows = [(s.protocol, s.pool, s.block_number, s.timestamp, s.tx_hash, s.log_index, s.event_sender, s.recipient,
                     s.tx_from, s.amount0, s.amount1, s.direction) for s in chunk if s.tx_from and s.timestamp]
            cur.executemany(INSERT, rows)
            cur.execute("INSERT INTO backfill_chunks (lo, hi, swaps, seconds) VALUES (%s,%s,%s,%s) ON CONFLICT (lo) DO UPDATE SET swaps = EXCLUDED.swaps, seconds = EXCLUDED.seconds, done_at = now()",
                        (lo, hi, len(rows), time.time() - t))
            conn.commit()
            with lock:
                stats["chunks"] += 1; stats["blocks"] += hi - lo + 1; stats["swaps"] += len(rows)
                n = stats["chunks"]
            if n % 10 == 0 or n == len(todo):
                rate = stats["blocks"] / (time.time() - t0)
                left = (total_blocks - stats["blocks"]) / rate / 3600 if rate else float("inf")
                log(f"{n:,}/{len(todo):,} chunks, {stats['swaps']:,} swaps, {rate:.0f} blocks/s, ~{left:.1f} h left, "
                    f"errors {stats['errors']}, throttled {rpc.throttled}x")

threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(WORKERS)]
for th in threads:
    th.start()
try:
    for th in threads:
        while th.is_alive():
            th.join(1)
except KeyboardInterrupt:
    stop.set(); log("stopping after current chunks")
    for th in threads:
        th.join()
log(f"done: {stats['chunks']:,} chunks, {stats['swaps']:,} swaps in {(time.time() - t0) / 3600:.2f} h")
