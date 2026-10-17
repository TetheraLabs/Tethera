"""Fill a gap in the hot `swaps` table from HyperSync.

The live follower skips ahead when it falls more than MAX_LAG blocks behind (its log
says "GAP lo hi"). This script loads those blocks the same way the archive backfill does,
but into `swaps`, with `is_automated` looked up from wallet_features.

Usage: python scripts/gapfill_hypersync.py --from BLOCK --to BLOCK
       python scripts/gapfill_hypersync.py --auto      # find gaps > 100 blocks in the last 2 days and fill them
"""
import asyncio, os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
import psycopg
import hypersync
from hypersync import LogField as LF, TransactionField as TF, BlockField as BF
import backfill_hypersync as bh      # decoder, topics, env

log = bh.log
DB = bh.DB

INSERT = """
INSERT INTO swaps (protocol, pool, block_number, block_time, tx_hash, log_index, event_sender, recipient,
                   tx_from, amount0, amount1, direction, is_automated)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,(SELECT is_automated FROM wallet_features WHERE wallet = %s))
ON CONFLICT (tx_hash, log_index) DO NOTHING
"""

def find_gaps(min_gap=100):
    with psycopg.connect(DB) as conn:
        rows = conn.execute("""
            WITH b AS (SELECT DISTINCT block_number FROM swaps WHERE block_time > now() - interval '2 days'),
                 g AS (SELECT block_number, lead(block_number) OVER (ORDER BY block_number) AS nxt FROM b)
            SELECT block_number + 1, nxt - 1 FROM g WHERE nxt - block_number > %s ORDER BY 1""", (min_gap,)).fetchall()
    return [(int(a), int(b)) for a, b in rows]

async def fill(lo, hi):
    client = hypersync.HypersyncClient(hypersync.ClientConfig(url=bh.URL, bearer_token=bh.TOKEN, proactive_rate_limit_sleep=True, max_num_retries=20))
    fs = hypersync.FieldSelection(log=[LF.BLOCK_NUMBER, LF.LOG_INDEX, LF.TRANSACTION_HASH, LF.ADDRESS, LF.TOPIC0, LF.TOPIC1, LF.TOPIC2, LF.DATA],
                                  transaction=[TF.HASH, TF.FROM], block=[BF.NUMBER, BF.TIMESTAMP])
    q = hypersync.Query(from_block=lo, to_block=hi + 1, logs=[hypersync.LogSelection(topics=[[bh.T_V2, bh.T_V3, bh.T_V4]])], field_selection=fs)
    stream = await client.stream_arrow(q, hypersync.StreamConfig(concurrency=2, hex_output=hypersync.HexOutput.PREFIXED))
    n = 0; t0 = time.time()
    with psycopg.connect(DB) as conn, conn.cursor() as cur:
        while True:
            r = await stream.recv()
            if r is None:
                break
            tx_from = dict(zip(r.data.transactions.column("hash").to_pylist(), r.data.transactions.column("from").to_pylist()))
            ts = {b["number"]: int(b["timestamp"], 16) for b in r.data.blocks.to_pylist()}
            rows = []
            for lg in r.data.logs.to_pylist():
                row = bh.decode(lg, tx_from, ts)
                if row:
                    rows.append(row + (row[8],))
            if rows:
                cur.executemany(INSERT, rows); conn.commit(); n += len(rows)
            log(f"gap {lo:,}-{hi:,}: at block {r.next_block:,}, {n:,} swaps, {n/(time.time()-t0):,.0f}/s")
    return n

if __name__ == "__main__":
    if "--auto" in sys.argv:
        gaps = find_gaps(); log(f"{len(gaps)} gaps: {gaps}")
    else:
        gaps = [(bh.arg("--from", None, int), bh.arg("--to", None, int))]
    total = 0
    for lo, hi in gaps:
        total += asyncio.run(fill(lo, hi))
    log(f"filled {total:,} swaps")
