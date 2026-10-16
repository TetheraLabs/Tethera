"""Resumable historical backfill of Uniswap swaps, newest-first, straight into Postgres.

Walks backwards from the oldest block already in `swaps` or `swaps_archive` down to --to (default:
block 653325, the first block of 2026-07-01 UTC). Writes to `swaps_archive`, the cold table on the
external drive; the hot `swaps` table on the Mini only holds the live tail. Progress is the table
itself: on restart it resumes from min(block_number). Each chunk is one transaction, so a crash
never leaves a half-written chunk. Set ARCHIVE_RPC_URL in .env to use a paid archive endpoint.
"""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import psycopg
from tethera.rpc import Rpc
from tethera import swaps as sw

TO = int(sys.argv[sys.argv.index("--to") + 1]) if "--to" in sys.argv else 653_325
CH = 2_000
from dotenv import load_dotenv; load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
ARCHIVE_RPC = os.environ.get("ARCHIVE_RPC_URL")
rpc = Rpc(url=ARCHIVE_RPC, min_interval=float(os.environ.get("BACKFILL_MIN_INTERVAL", "0.05" if ARCHIVE_RPC else "0.5")), retries=60)
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)
DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")

with psycopg.connect(DB) as conn, conn.cursor() as cur:
    cur.execute("SELECT least(min(s.b), min(a.b)) FROM (SELECT min(block_number) b FROM swaps) s, (SELECT min(block_number) b FROM swaps_archive) a"); oldest = cur.fetchone()[0]
    hi = oldest - 1
    log(f"backfilling from block {hi} down to {TO} ({hi - TO:,} blocks) via {'archive rpc' if ARCHIVE_RPC else 'public rpc'} into swaps_archive")
    t0 = time.time(); done_blocks = 0; n = 0
    while hi >= TO:
        lo = max(hi - CH + 1, TO)
        try:
            chunk = sw.fetch_swaps(rpc, lo, hi, chunk=CH)
            sw.attach_tx_from(rpc, chunk, batch_size=25)
        except Exception as e:
            log(f"blocks {lo}-{hi}: {type(e).__name__}: {str(e)[:80]}; retry in 60s"); time.sleep(60); continue
        rows = [(s.protocol, s.pool, s.block_number, s.timestamp, s.tx_hash, s.log_index, s.event_sender, s.recipient,
                 s.tx_from, s.amount0, s.amount1, s.direction) for s in chunk if s.tx_from and s.timestamp]
        cur.executemany("""INSERT INTO swaps_archive (protocol, pool, block_number, block_time, tx_hash, log_index, event_sender,
                           recipient, tx_from, amount0, amount1, direction)
                           VALUES (%s,%s,%s,to_timestamp(%s),%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (tx_hash, log_index) DO NOTHING""", rows)
        conn.commit()
        n += len(rows); done_blocks += hi - lo + 1; hi = lo - 1
        if done_blocks % (CH * 10) == 0:
            rate = done_blocks / (time.time() - t0)
            eta_h = (hi - TO) / rate / 3600 if rate else float("inf")
            log(f"at block {hi:,}: {n:,} swaps so far, {rate:.0f} blocks/s, ~{eta_h:.0f} h to go, throttled {rpc.throttled}x")
    log(f"backfill complete: {n:,} swaps")
