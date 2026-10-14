"""Fill the pools table for every v2/v3 pool that appears in swaps, via eth_call on a non-archive endpoint."""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from dotenv import load_dotenv; load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
import psycopg
from tethera.rpc import Rpc
from tethera import swaps as sw

rpc = Rpc(os.environ["CHAINSTACK_RPC_URL"], min_interval=2.0)
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)
with psycopg.connect(os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")) as conn, conn.cursor() as cur:
    cur.execute("""SELECT DISTINCT s.pool, s.protocol FROM swaps s LEFT JOIN pools p ON p.pool = s.pool
                   WHERE s.protocol IN ('v2','v3') AND p.pool IS NULL""")
    todo = cur.fetchall(); log(f"{len(todo)} v2/v3 pools to resolve")
    for i in range(0, len(todo), 5):
        chunk = todo[i:i + 5]
        try:
            toks = sw.fetch_pool_tokens(rpc, [p for p, _ in chunk], batch_size=10)
        except Exception as e:
            log(f"error at {i}: {str(e)[:100]}; sleeping 5s"); time.sleep(5); continue
        for p, proto in chunk:
            if p in toks:
                cur.execute("INSERT INTO pools (pool, protocol, token0, token1) VALUES (%s,%s,%s,%s) ON CONFLICT (pool) DO NOTHING", (p, proto, *toks[p]))
        conn.commit()
        if (i // 5) % 200 == 0: log(f"{i + len(chunk)}/{len(todo)}")
    log("pool tokens complete")
