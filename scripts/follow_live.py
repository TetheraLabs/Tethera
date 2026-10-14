"""Follow the chain head and append every Uniswap swap, with its transaction sender, to data/swaps_live.jsonl.

Designed for a non-archive endpoint (the paid provider free plan) that only serves the
most recent ~100 blocks: polls every second or two and never falls far behind.
Rate-limited to stay under the plan's requests-per-second cap.
"""
import json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from dotenv import load_dotenv
load_dotenv()
from tethera.rpc import Rpc
from tethera import swaps as sw

URL = os.environ["PAID_RPC_URL"]
OUT = "data/swaps_live.jsonl"
MAX_LAG = 60          # blocks; older than this the free plan refuses
BLOCKS_PER_POLL = 20  # keep each getLogs + block batch small
rpc = Rpc(URL, min_interval=0.08)  # ~12 req/s, under the 25 rps cap
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)

head = rpc.block_number()
next_block = head - 10
n = 0; t0 = time.time(); last_report = t0
log(f"starting at block {next_block}")
while True:
    try:
        head = rpc.block_number()
        if head - next_block > MAX_LAG:
            log(f"fell {head - next_block} blocks behind; skipping to head-{MAX_LAG}")
            next_block = head - MAX_LAG
        if next_block > head - 2:           # stay 2 blocks behind head to avoid reorgs/races
            time.sleep(1.0); continue
        a, b = next_block, min(next_block + BLOCKS_PER_POLL - 1, head - 2)
        chunk = sw.fetch_swaps(rpc, a, b, chunk=BLOCKS_PER_POLL)
        sw.attach_tx_from(rpc, chunk, batch_size=10)
        with open(OUT, "a") as f:
            for s in chunk:
                f.write(json.dumps(sw.to_row(s)) + "\n")
        n += len(chunk); next_block = b + 1
        if time.time() - last_report > 60:
            log(f"at block {next_block}, head {head}, lag {head - next_block}, {n} swaps total, {n / (time.time() - t0) * 60:.0f}/min")
            last_report = time.time()
    except KeyboardInterrupt:
        break
    except Exception as e:
        log(f"error: {type(e).__name__}: {str(e)[:120]}; retrying in 3s")
        time.sleep(3)
