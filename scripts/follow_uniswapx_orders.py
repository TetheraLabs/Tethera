"""Poll the UniswapX order feed for chain 4663 and record every order and its eventual status."""
import json, os, sys, time, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from datetime import datetime, timezone
import psycopg
from psycopg.types.json import Jsonb

API = "https://api.uniswap.org/v2/orders?chainId=4663&limit=100"
H = {"user-agent": "tethera/0.1"}
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)

def fetch(status=None):
    url = API + (f"&orderStatus={status}" if status else "")
    with urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=30) as r:
        return json.load(r).get("orders", [])

def upsert(cur, o):
    inp = o.get("input") or {}; outs = o.get("outputs") or [{}]; out = outs[0]
    curve = (inp.get("curve") or {}).get("relativeBlocks") or []
    cur.execute("""
        INSERT INTO uniswapx_orders (order_hash, created_at, swapper, input_token, input_start, input_max, output_token,
            output_start, output_min, output_recipient, decay_blocks, cosigned, last_status, fill_tx, fill_block, raw)
        VALUES (%s, to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (order_hash) DO UPDATE SET last_status = EXCLUDED.last_status,
            fill_tx = COALESCE(EXCLUDED.fill_tx, uniswapx_orders.fill_tx),
            fill_block = COALESCE(EXCLUDED.fill_block, uniswapx_orders.fill_block),
            raw = CASE WHEN EXCLUDED.last_status <> 'open' THEN EXCLUDED.raw ELSE uniswapx_orders.raw END
    """, (o.get("orderHash"), o.get("createdAt"), o.get("swapper"), inp.get("token"), inp.get("startAmount"), inp.get("maxAmount"),
          out.get("token"), out.get("startAmount"), out.get("minAmount"), out.get("recipient"),
          curve[-1] if curve else None, bool(o.get("cosignature")), o.get("orderStatus", "open"), o.get("txHash"), o.get("fillBlock"), Jsonb(o)))

with psycopg.connect(os.environ.get("DATABASE_URL", "postgresql://localhost/tethera"), autocommit=True) as conn, conn.cursor() as cur:
    n = 0; last = time.time()
    while True:
        try:
            for status in ("open", None):      # open ones first, then the latest of any status to catch fills/expiries
                for o in fetch(status):
                    upsert(cur, o); n += 1
            if time.time() - last > 600:
                cur.execute("SELECT count(*), count(*) FILTER (WHERE last_status='open'), count(*) FILTER (WHERE last_status='filled') FROM uniswapx_orders")
                t, op, f = cur.fetchone(); log(f"{t} orders recorded ({op} open, {f} filled), {n} upserts"); last = time.time()
        except Exception as e:
            log(f"error: {type(e).__name__}: {str(e)[:100]}"); time.sleep(10)
        time.sleep(3)
