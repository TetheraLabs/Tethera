"""Historical backfill of Uniswap swaps into `swaps_archive` from Envio HyperSync.

HyperSync returns swap logs already joined to their transaction (for the sender) and block (for the
timestamp), so the whole history is a few tens of thousands of responses instead of one block fetch
per block. Rows are written with COPY. Progress is recorded in `backfill_chunks` (one row per
response range) inside the same transaction as the rows, so a restart resumes exactly where it stopped.

For the full load the secondary indexes and the unique (block_number, log_index) key are dropped first and rebuilt at the end
(random index inserts on a spinning disk are the slow part). While they are down the nightly
archive job is unloaded, because the nightly move needs the unique key.

  --from BLOCK      first block (default 653325 = first block of 2026-07-01 UTC)
  --to BLOCK        last block (default: one below the oldest block already in swaps_archive)
  --keep-indexes    do not drop/rebuild indexes (for small test ranges)
  --finish          only rebuild indexes, analyse and reload the archive job
  --concurrency N   parallel HyperSync requests (default 6)

Needs HYPERSYNC_URL and HYPERSYNC_TOKEN in .env.
"""
import asyncio, os, sys, time, subprocess
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import psycopg
from dotenv import load_dotenv
from web3 import Web3
import hypersync
from hypersync import LogField as LF, TransactionField as TF, BlockField as BF

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

def arg(name, default, cast=str):
    return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default

FROM = arg("--from", 653_325, int)
TO_ARG = arg("--to", None, int)
KEEP = "--keep-indexes" in sys.argv
FINISH = "--finish" in sys.argv
CONC = arg("--concurrency", 6, int)
DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
URL = os.environ["HYPERSYNC_URL"]; TOKEN = os.environ["HYPERSYNC_TOKEN"]
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.tethera.archive.plist")
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)

T_V2 = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
T_V3 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
T_V4 = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
COLS = "protocol, pool, block_number, block_time, tx_hash, log_index, event_sender, recipient, tx_from, amount0, amount1, direction"
INDEXES = {  # name -> rebuild statement
    "swaps_archive_pool_time_idx": "CREATE INDEX swaps_archive_pool_time_idx ON swaps_archive (pool, block_time) TABLESPACE tethera_archive",
    "swaps_archive_from_idx": "CREATE INDEX swaps_archive_from_idx ON swaps_archive (tx_from) TABLESPACE tethera_archive",
    "swaps_archive_block_idx": "CREATE INDEX swaps_archive_block_idx ON swaps_archive (block_number) TABLESPACE tethera_archive",
    "swaps_archive_block_log_key": "CREATE UNIQUE INDEX swaps_archive_block_log_key ON swaps_archive (block_number, log_index) TABLESPACE tethera_archive",
}

_ck: dict[str, str] = {}
def ck(addr_hex: str) -> str:
    """Checksummed address, cached (the hot and archive tables store checksummed addresses)."""
    a = _ck.get(addr_hex)
    if a is None:
        a = _ck[addr_hex] = Web3.to_checksum_address(addr_hex)
        if len(_ck) > 2_000_000:
            _ck.clear()
    return a

def s256(b: bytes) -> int: return int.from_bytes(b, "big", signed=True)
def u256(b: bytes) -> int: return int.from_bytes(b, "big")
def topic_addr(t: str) -> str: return ck("0x" + t[-40:])

def decode(lg: dict, tx_from: dict, ts: dict):
    t0 = lg["topic0"]; data = bytes.fromhex(lg["data"][2:]); bn = lg["block_number"]
    if t0 == T_V2:
        a0in, a1in, a0out, a1out = u256(data[0:32]), u256(data[32:64]), u256(data[64:96]), u256(data[96:128])
        amount0, amount1 = a0in - a0out, a1in - a1out
        proto, pool, sender, rcpt = "v2", ck(lg["address"]), topic_addr(lg["topic1"]), topic_addr(lg["topic2"])
    elif t0 == T_V3:
        amount0, amount1 = s256(data[0:32]), s256(data[32:64])
        proto, pool, sender, rcpt = "v3", ck(lg["address"]), topic_addr(lg["topic1"]), topic_addr(lg["topic2"])
    elif t0 == T_V4:
        amount0, amount1 = -s256(data[0:32]), -s256(data[32:64])   # v4 convention inverted
        proto, pool, sender, rcpt = "v4", lg["topic1"], topic_addr(lg["topic2"]), None
    else:
        return None
    frm = tx_from.get(lg["transaction_hash"]); t = ts.get(bn)
    if frm is None or t is None:
        return None
    return (proto, pool, bn, datetime.fromtimestamp(t, timezone.utc), lg["transaction_hash"], lg["log_index"],
            sender, rcpt, ck(frm), amount0, amount1, "sell0" if amount0 > 0 else "buy0")

def pg_exec(sql_list):
    with psycopg.connect(DB, autocommit=True) as conn:
        for s in sql_list:
            log("sql:", s[:90]); t = time.time(); conn.execute(s); log(f"  done in {time.time()-t:,.0f}s")

def drop_indexes():
    with psycopg.connect(DB) as conn:
        have = {r[0] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE tablename='swaps_archive'")}
    stmts = [f"DROP INDEX IF EXISTS {n}" for n in INDEXES if n in have]
    if stmts:
        subprocess.run(["launchctl", "unload", PLIST], check=False); log("archive job unloaded while the unique key is down")
        pg_exec(stmts)

def finish():
    with psycopg.connect(DB) as conn:
        have = {r[0] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE tablename='swaps_archive'")}
    stmts = ["SET maintenance_work_mem = '2GB'", "SET temp_tablespaces = 'tethera_archive'"]
    stmts += [s for n, s in INDEXES.items() if n not in have]
    stmts.append("ANALYZE swaps_archive")
    with psycopg.connect(DB, autocommit=True) as conn:
        for s in stmts:
            log("sql:", s[:90]); t = time.time(); conn.execute(s); log(f"  done in {time.time()-t:,.0f}s")
    subprocess.run(["launchctl", "load", PLIST], check=False); log("archive job reloaded")

async def run():
    with psycopg.connect(DB) as conn:
        if TO_ARG is not None:
            to = TO_ARG
        else:
            (amin,) = conn.execute("SELECT min(block_number) FROM swaps_archive WHERE block_number > %s", (FROM + 10_000_000,)).fetchone()
            to = (amin - 1) if amin else conn.execute("SELECT min(block_number) - 1 FROM swaps").fetchone()[0]
        (done_hi,) = conn.execute("SELECT max(hi) FROM backfill_chunks WHERE lo >= %s AND hi <= %s", (FROM, to)).fetchone()
    start = (done_hi + 1) if done_hi else FROM
    if start > to:
        log(f"nothing to do: {FROM:,}-{to:,} already loaded"); return True
    log(f"hypersync backfill {start:,} -> {to:,} ({to-start+1:,} blocks), concurrency {CONC}, indexes {'kept' if KEEP else 'dropped for the load'}")
    if not KEEP:
        drop_indexes()
    client = hypersync.HypersyncClient(hypersync.ClientConfig(url=URL, bearer_token=TOKEN, proactive_rate_limit_sleep=True, max_num_retries=20))
    fs = hypersync.FieldSelection(log=[LF.BLOCK_NUMBER, LF.LOG_INDEX, LF.TRANSACTION_HASH, LF.ADDRESS, LF.TOPIC0, LF.TOPIC1, LF.TOPIC2, LF.DATA],
                                  transaction=[TF.HASH, TF.FROM], block=[BF.NUMBER, BF.TIMESTAMP])
    q = hypersync.Query(from_block=start, to_block=to + 1, logs=[hypersync.LogSelection(topics=[[T_V2, T_V3, T_V4]])], field_selection=fs)
    stream = await client.stream_arrow(q, hypersync.StreamConfig(concurrency=CONC, hex_output=hypersync.HexOutput.PREFIXED))
    t0 = time.time(); cursor = start; n_rows = n_resp = skipped = 0
    with psycopg.connect(DB) as conn, conn.cursor() as cur:
        while True:
            r = await stream.recv()
            if r is None:
                break
            tx_from = dict(zip(r.data.transactions.column("hash").to_pylist(), r.data.transactions.column("from").to_pylist()))
            ts = {b["number"]: int(b["timestamp"], 16) for b in r.data.blocks.to_pylist()}
            rows = []
            for lg in r.data.logs.to_pylist():
                row = decode(lg, tx_from, ts)
                if row: rows.append(row)
                else: skipped += 1
            hi = min(r.next_block - 1, to)
            with cur.copy(f"COPY swaps_archive ({COLS}) FROM STDIN") as cp:
                for row in rows:
                    cp.write_row(row)
            cur.execute("INSERT INTO backfill_chunks (lo, hi, swaps, seconds) VALUES (%s,%s,%s,%s) ON CONFLICT (lo) DO UPDATE SET hi=EXCLUDED.hi, swaps=EXCLUDED.swaps, seconds=EXCLUDED.seconds, done_at=now()",
                        (cursor, hi, len(rows), 0.0))
            conn.commit()
            n_rows += len(rows); n_resp += 1; cursor = hi + 1
            if n_resp % 50 == 0:
                el = time.time() - t0; rate = (cursor - start) / el
                left = (to - cursor + 1) / rate / 3600 if rate else float("inf")
                log(f"block {cursor:,} / {to:,}  {n_rows:,} swaps  {rate:,.0f} blocks/s  {n_rows/el:,.0f} swaps/s  ~{left:.1f} h left  skipped {skipped}")
            if cursor > to:
                break
    log(f"loaded {n_rows:,} swaps to block {cursor-1:,} in {(time.time()-t0)/3600:.2f} h, skipped {skipped}")
    return cursor > to

if __name__ == "__main__":
    if FINISH:
        finish(); sys.exit(0)
    complete = asyncio.run(run())
    if complete and not KEEP:
        finish()
