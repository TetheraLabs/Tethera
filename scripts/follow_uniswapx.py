"""Follow the chain head and record every Fill event on the UniswapX V3DutchOrderReactor into Postgres."""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from dotenv import load_dotenv; load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
import psycopg
from web3 import Web3
from tethera.rpc import Rpc

REACTOR = "0x000000007A1C8e570011EeDF86A2A35593013cBA"
TOPIC_FILL = "0x" + Web3.keccak(text="Fill(bytes32,address,address,uint256)").hex().removeprefix("0x")
rpc = Rpc(os.environ["CHAINSTACK_RPC_URL"], min_interval=0.5)
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)
MAX_LAG, STEP = 60, 40

with psycopg.connect(os.environ.get("DATABASE_URL", "postgresql://localhost/tethera"), autocommit=True) as conn, conn.cursor() as cur:
    head = rpc.block_number(); nxt = head - 10; n = 0; last = time.time()
    log(f"starting at block {nxt}")
    while True:
        try:
            head = rpc.block_number()
            if head - nxt > MAX_LAG:
                nxt = head - MAX_LAG
            if nxt > head - 2:
                time.sleep(2); continue
            a, b = nxt, min(nxt + STEP - 1, head - 2)
            logs = rpc.get_logs(a, b, address=REACTOR, topics=[TOPIC_FILL])
            if logs:
                ts = {}
                for blk in rpc.batch([("eth_getBlockByNumber", [hex(x), False]) for x in sorted({int(l["blockNumber"], 16) for l in logs})]):
                    ts[int(blk["number"], 16)] = int(blk["timestamp"], 16)
                for l in logs:
                    bn = int(l["blockNumber"], 16)
                    cur.execute("""INSERT INTO uniswapx_fills (tx_hash, log_index, block_number, block_time, order_hash, filler, swapper, nonce)
                                   VALUES (%s,%s,%s,to_timestamp(%s),%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                                (l["transactionHash"], int(l["logIndex"], 16), bn, ts.get(bn), l["topics"][1],
                                 Web3.to_checksum_address("0x" + l["topics"][2][-40:]), Web3.to_checksum_address("0x" + l["topics"][3][-40:]),
                                 int(l["data"], 16) if l["data"] and l["data"] != "0x" else None))
                n += len(logs)
            nxt = b + 1
            if time.time() - last > 300:
                log(f"at block {nxt}, lag {head - nxt}, {n} fills recorded"); last = time.time()
        except KeyboardInterrupt:
            break
        except Exception as e:
            log(f"error: {type(e).__name__}: {str(e)[:100]}; retrying in 5s"); time.sleep(5)
