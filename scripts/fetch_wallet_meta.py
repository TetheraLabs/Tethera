"""Fill wallet_features.lifetime_tx_count and is_contract via the Chainstack endpoint (latest state only, cheap)."""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from dotenv import load_dotenv; load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
import psycopg
from tethera.rpc import Rpc

rpc = Rpc(os.environ["CHAINSTACK_RPC_URL"], min_interval=0.5)   # gentle: shares the plan's RPS cap with the live follower
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)
with psycopg.connect(os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")) as conn, conn.cursor() as cur:
    cur.execute("SELECT wallet FROM wallet_features WHERE lifetime_tx_count IS NULL ORDER BY n_swaps DESC")
    wallets = [r[0] for r in cur.fetchall()]
    log(f"{len(wallets)} wallets to look up")
    B = 5
    for i in range(0, len(wallets), B):
        chunk = wallets[i:i + B]
        try:
            nonces = rpc.batch([("eth_getTransactionCount", [w, "latest"]) for w in chunk])
            codes = rpc.batch([("eth_getCode", [w, "latest"]) for w in chunk])
        except Exception as e:
            log(f"error at {i}: {str(e)[:100]}; sleeping 5s"); time.sleep(5); continue
        for w, n, c in zip(chunk, nonces, codes):
            cur.execute("UPDATE wallet_features SET lifetime_tx_count = %s, is_contract = %s WHERE wallet = %s",
                        (int(n, 16), bool(c and c != "0x"), w))
        conn.commit()
        if (i // B) % 100 == 0:
            log(f"{i + len(chunk)}/{len(wallets)} done, throttled {rpc.throttled}x")
    log("wallet meta complete")
