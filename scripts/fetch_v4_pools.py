"""Load every Uniswap v4 pool (PoolManager Initialize events) into the pools table. Needs an archive-capable RPC."""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import psycopg
from tethera.rpc import Rpc
from tethera import swaps as sw

rpc = Rpc(min_interval=1.0, retries=60)
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)
latest = rpc.block_number()
with psycopg.connect(os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")) as conn, conn.cursor() as cur:
    CH = 2_000_000; n = 0
    for a in range(1, latest + 1, CH):
        b = min(a + CH - 1, latest)
        while True:
            try:
                pools = sw.fetch_v4_pools(rpc, a, b, chunk=CH); break
            except Exception as e:
                log(f"blocks {a}-{b}: {str(e)[:80]}; retry in 60s"); time.sleep(60)
        cur.executemany("""INSERT INTO pools (pool, protocol, token0, token1, fee, tick_spacing, hooks, created_block)
                           VALUES (%s,'v4',%s,%s,%s,%s,%s,%s) ON CONFLICT (pool) DO NOTHING""",
                        [(pid, d["currency0"], d["currency1"], d["fee"], d["tick_spacing"], d["hooks"], d["block_number"]) for pid, d in pools.items()])
        conn.commit(); n += len(pools)
        log(f"blocks {a}-{b}: {len(pools)} pools (total {n})")
    log(f"v4 pools complete: {n}")
