"""Follow the chain head and insert every Uniswap swap, with its transaction sender, straight into Postgres.

Designed for a non-archive endpoint that only serves the most recent ~100 blocks:
polls every second or two and never falls far behind. Falls over to the public
endpoint when the paid one refuses (quota exhausted, key revoked) and back again
later. Blocks skipped while catching up are logged and filled by gapfill_hypersync.py.
"""
import json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from dotenv import load_dotenv
load_dotenv()
from tethera.rpc import live_rpc
from tethera import swaps as sw
from tethera import db

MAX_LAG = 60          # blocks; older than this the paid non-archive plan refuses
BLOCKS_PER_POLL = 20  # keep each getLogs + block batch small
CATCHUP_POLL = 100    # bigger polls while behind on an endpoint that serves history
LIMITED = os.environ.get("PAID_RPC_URL")   # the only endpoint that refuses old blocks
rpc = live_rpc()  # paid endpoint first, public endpoint when it fails
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)

conn = db.connect(autocommit=True); cur = conn.cursor()
head = rpc.block_number()
next_block = head - 10
n = 0; t0 = time.time(); last_report = t0
log(f"starting at block {next_block}")
while True:
    try:
        head = rpc.block_number()
        if head - next_block > MAX_LAG and rpc.active == LIMITED:
            log(f"GAP {next_block} {head - MAX_LAG - 1}: fell {head - next_block} blocks behind; skipping to head-{MAX_LAG}")
            next_block = head - MAX_LAG
        if next_block > head - 2:           # stay 2 blocks behind head to avoid reorgs/races
            time.sleep(1.0); continue
        per_poll = CATCHUP_POLL if head - next_block > 2 * BLOCKS_PER_POLL else BLOCKS_PER_POLL
        a, b = next_block, min(next_block + per_poll - 1, head - 2)
        chunk = sw.fetch_swaps(rpc, a, b, chunk=per_poll)
        sw.attach_tx_from(rpc, chunk, batch_size=20)
        n += db.insert_swaps(cur, chunk)
        if chunk: cur.execute("SELECT flag_self_cancelling('swaps', %s, %s)", (a, b))   # one tx, one pool, both ways, nothing settled
        next_block = b + 1
        if time.time() - last_report > 60:
            log(f"at block {next_block}, head {head}, lag {head - next_block}, {n} swaps total, {n / (time.time() - t0) * 60:.0f}/min")
            last_report = time.time()
    except KeyboardInterrupt:
        break
    except Exception as e:
        log(f"error: {type(e).__name__}: {str(e)[:120]}; retrying in 3s")
        time.sleep(3)
