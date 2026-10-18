"""Fill the pools table for pools that have traded but are missing from it.

v2 and v3 pools: token0(), token1() and fee() by eth_call (v2 has a fixed 0.3% fee).
v4 pools: PoolManager Initialize events from the last known created_block to the head.
Idempotent; run after the follower has seen new pools. Usage: python scripts/fetch_pool_meta.py [--days N] [--limit N]
"""
import argparse, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import psycopg
from tethera.rpc import live_rpc, RpcError
from tethera import swaps as sw

DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)
SEL = {"token0": "0x0dfe1681", "token1": "0xd21220a7", "fee": "0xddca3f43"}
ap = argparse.ArgumentParser(); ap.add_argument("--days", type=int, default=None, help="only pools that traded in the last N days (default: all)")
ap.add_argument("--limit", type=int, default=None); ap.add_argument("--batch", type=int, default=5); ap.add_argument("--v4-chunk", type=int, default=50_000)
a = ap.parse_args()
rpc = live_rpc()

def addr(word): return "0x" + word[-40:] if word and len(word) >= 42 else None

with psycopg.connect(DB) as conn, conn.cursor() as cur:
    cur.execute("SET statement_timeout = '10min'")
    since = "AND block_time >= now() - (%s || ' days')::interval" if a.days else ""
    cur.execute(f"""SELECT s.pool, s.protocol, count(*) FROM swaps s LEFT JOIN pools p ON p.pool = s.pool
                    WHERE p.pool IS NULL AND s.protocol IN ('v2', 'v3') {since} GROUP BY 1, 2 ORDER BY 3 DESC {"LIMIT %s" if a.limit else ""}""",
                tuple(x for x in [a.days, a.limit] if x))
    todo = cur.fetchall(); log(f"{len(todo):,} v2/v3 pools to fetch")
    n = bad = 0; t = time.time()
    for i in range(0, len(todo), a.batch):
        group = todo[i:i + a.batch]
        calls = [("eth_call", [{"to": pool, "data": d}, "latest"]) for pool, proto, _ in group for d in (SEL["token0"], SEL["token1"], SEL["fee"])]
        res = None
        for attempt in range(6):   # the public RPC rate-limits batches; back off rather than give up
            try: res = rpc.batch(calls, lenient=True); break
            except Exception as e:
                log(f"batch failed ({str(e)[:60]}), retry {attempt+1} in {15*(attempt+1)}s"); time.sleep(15 * (attempt + 1))
        if res is None: bad += len(group); continue
        rows = []
        for j, (pool, proto, _) in enumerate(group):
            t0, t1, fee = res[3 * j:3 * j + 3]
            t0, t1 = (addr(x) if isinstance(x, str) else None for x in (t0, t1))
            if not t0 or not t1: bad += 1; continue
            fee = int(fee, 16) if proto == "v3" and isinstance(fee, str) and len(fee) > 2 else 3000
            rows.append((pool, proto, sw.Web3.to_checksum_address(t0), sw.Web3.to_checksum_address(t1), fee))
        cur.executemany("INSERT INTO pools (pool, protocol, token0, token1, fee) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (pool) DO NOTHING", rows)
        conn.commit(); n += len(rows)
        if (i // a.batch) % 100 == 0: log(f"  {n:,} inserted, {bad} unreadable, {time.time()-t:.0f}s")
    log(f"v2/v3 done: {n:,} inserted, {bad} unreadable ({time.time()-t:.0f}s)")

    cur.execute("SELECT coalesce(max(created_block), 0) FROM pools WHERE protocol = 'v4'"); start = cur.fetchone()[0] + 1
    head = rpc.block_number(); log(f"v4 Initialize events from block {start:,} to {head:,}")
    chunk, m, a_ = a.v4_chunk, 0, start
    while a_ <= head:
        b = min(a_ + chunk - 1, head)
        try: pools = sw.fetch_v4_pools(rpc, a_, b, chunk=chunk)
        except RpcError as e:
            if chunk > 2_000: chunk //= 2; log(f"  range too big, chunk now {chunk:,}"); continue
            log(f"  blocks {a_}-{b} failed: {str(e)[:80]}"); a_ = b + 1; continue
        cur.executemany("""INSERT INTO pools (pool, protocol, token0, token1, fee, tick_spacing, hooks, created_block)
                           VALUES (%s,'v4',%s,%s,%s,%s,%s,%s) ON CONFLICT (pool) DO NOTHING""",
                        [(pid, d["currency0"], d["currency1"], d["fee"], d["tick_spacing"], d["hooks"], d["block_number"]) for pid, d in pools.items()])
        conn.commit(); m += len(pools); a_ = b + 1
    log(f"v4 done: {m:,} pools")
