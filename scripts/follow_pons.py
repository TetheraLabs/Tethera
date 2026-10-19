"""Follow the Pons launchpad: launches, bonding-curve buys and sells, and graduations.

Before a token graduates to Uniswap it trades on its own curve contract, which no Uniswap swap reader sees. This follower
reads the Pons events straight from the chain into `launches` and `curve_trades`, so the launch audit covers a token's
first minutes, not just its life after graduation.

Events (Pons V2, Robinhood Chain):
  TokenLaunched(address indexed token, address indexed curve, address indexed deployer, address pairToken, uint256 launchConfigId, uint256 graduationThreshold)
  CurveBuy(address indexed buyer, address indexed recipient, uint256 quoteIn, uint256 tokensOut, uint256 fee, uint256 tax)
  CurveSell(address indexed seller, address indexed recipient, uint256 tokensIn, uint256 quoteOut, uint256 fee, uint256 tax)
  PoolGraduated(address indexed token, uint256 positionId, uint256 tokenAmount, uint256 pairTokenAmount)

Usage: python scripts/follow_pons.py                 # follow the head, resuming from the last stored block
       python scripts/follow_pons.py --from B --to B # one range, then exit (backfill)
"""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from dotenv import load_dotenv; load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
import psycopg
from web3 import Web3
from tethera.rpc import live_rpc

DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
FACTORY = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
T_LAUNCH = Web3.keccak(text="TokenLaunched(address,address,address,address,uint256,uint256)").hex()
T_BUY = Web3.keccak(text="CurveBuy(address,address,uint256,uint256,uint256,uint256)").hex()
T_SELL = Web3.keccak(text="CurveSell(address,address,uint256,uint256,uint256,uint256)").hex()
T_GRAD = Web3.keccak(text="PoolGraduated(address,uint256,uint256,uint256)").hex()
h = lambda t: t if t.startswith("0x") else "0x" + t
TOPICS = [[h(T_LAUNCH), h(T_BUY), h(T_SELL), h(T_GRAD)]]
log = lambda *a: print(time.strftime("%m-%d %H:%M:%S"), *a, flush=True)
addr = lambda topic: Web3.to_checksum_address("0x" + topic[-40:])
def words(data):
    d = data[2:] if data.startswith("0x") else data
    return [int(d[i:i + 64], 16) for i in range(0, len(d), 64)]

def block_times(rpc, numbers):
    out = {}
    nums = sorted(set(numbers))
    for i in range(0, len(nums), 20):
        group = nums[i:i + 20]
        res = rpc.batch([("eth_getBlockByNumber", [hex(n), False]) for n in group], lenient=True)
        for n, b in zip(group, res):
            if b: out[n] = int(b["timestamp"], 16)
    return out

def process(rpc, conn, a, b):
    logs = rpc.get_logs(a, b, topics=TOPICS)
    if not logs: return 0
    ts = block_times(rpc, [int(l["blockNumber"], 16) for l in logs])
    n = 0
    with conn.cursor() as cur:
        for l in logs:
            t0 = l["topics"][0].lower(); bn = int(l["blockNumber"], 16); bt = ts.get(bn)
            if bt is None: continue
            tx, li = l["transactionHash"], int(l["logIndex"], 16); when = time.strftime("%Y-%m-%d %H:%M:%S+00", time.gmtime(bt))
            if t0 == h(T_LAUNCH).lower():
                w = words(l["data"]); pair = Web3.to_checksum_address("0x" + hex(w[0])[2:].rjust(40, "0"))
                cur.execute("""INSERT INTO launches (token, curve, deployer, pair_token, threshold, block_number, block_time, tx_hash)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (token) DO NOTHING""",
                            (addr(l["topics"][1]), addr(l["topics"][2]), addr(l["topics"][3]), pair, w[2], bn, when, tx)); n += 1
            elif t0 in (h(T_BUY).lower(), h(T_SELL).lower()):
                w = words(l["data"]); side = "buy" if t0 == h(T_BUY).lower() else "sell"
                quote, tokens = (w[0], w[1]) if side == "buy" else (w[1], w[0])
                curve = Web3.to_checksum_address(l["address"])
                cur.execute("""INSERT INTO curve_trades (tx_hash, log_index, curve, token, side, trader, recipient, quote_amount, token_amount, fee, tax, block_number, block_time)
                               VALUES (%s,%s,%s,(SELECT token FROM launches WHERE curve = %s),%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                            (tx, li, curve, curve, side, addr(l["topics"][1]), addr(l["topics"][2]), quote, tokens, w[2], w[3], bn, when)); n += 1
            elif t0 == h(T_GRAD).lower():
                cur.execute("UPDATE launches SET graduated_block = %s, graduated_time = %s WHERE token = %s", (bn, when, addr(l["topics"][1]))); n += 1
    conn.commit()
    return n

def main():
    rpc = live_rpc()
    with psycopg.connect(DB, autocommit=False) as conn:
        if "--from" in sys.argv:
            a = int(sys.argv[sys.argv.index("--from") + 1]); b = int(sys.argv[sys.argv.index("--to") + 1])
            total = 0
            for x in range(a, b + 1, 2000):
                y = min(x + 1999, b); total += process(rpc, conn, x, y)
                log(f"{x:,}-{y:,}: {total:,} rows so far")
            # trades stored before their launch row existed have no token yet
            conn.execute("UPDATE curve_trades c SET token = l.token FROM launches l WHERE l.curve = c.curve AND c.token IS NULL"); conn.commit()
            log(f"done: {total:,} rows"); return
        row = conn.execute("SELECT greatest(coalesce(max(block_number), 0), (SELECT coalesce(max(block_number), 0) FROM launches)) FROM curve_trades").fetchone()
        next_block = (row[0] or (rpc.block_number() - 500)) + 1
        log(f"following Pons from block {next_block:,}")
        total = 0; last = time.time()
        while True:
            head = rpc.block_number() - 2
            if next_block > head: time.sleep(2); continue
            b = min(next_block + 200, head)
            try:
                total += process(rpc, conn, next_block, b); next_block = b + 1
            except Exception as e:
                log(f"error: {type(e).__name__}: {str(e)[:100]}; retrying"); time.sleep(5); continue
            if time.time() - last > 60:
                log(f"at block {next_block:,}, head {head:,}, {total:,} rows"); last = time.time()

if __name__ == "__main__":
    main()
