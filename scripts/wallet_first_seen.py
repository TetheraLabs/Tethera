"""Wallet age: the first and last swap of every wallet, from swaps_archive and the hot table.

  --full               rebuild from scratch: one pass over swaps_archive, then merge the hot table
  --update             merge hot-table swaps newer than the table's last block (nightly)
  --max-block N        with --full, stop at block N (for a test on a small range)

The merge keeps the earliest first swap and the latest last swap per wallet, so running --update
twice over the same blocks is harmless for the dates (n_swaps would double, so it is not done).
A wallet's age for a launch audit is first_time, which is exact from the start of the archive
(1 July 2026) rather than from the start of the hot table.

Usage: python scripts/wallet_first_seen.py --full | --update
"""
import os, sys, time
import psycopg

DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
FULL = "--full" in sys.argv; UPDATE = "--update" in sys.argv
MAX_BLOCK = int(sys.argv[sys.argv.index("--max-block") + 1]) if "--max-block" in sys.argv else None
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)

MERGE = """
INSERT INTO wallet_first_seen (wallet, first_block, first_time, last_block, n_swaps)
SELECT tx_from, min(block_number), min(block_time), max(block_number), count(*)
FROM {table} WHERE block_number > %s AND block_number <= %s GROUP BY tx_from
ON CONFLICT (wallet) DO UPDATE SET
    first_block = least(wallet_first_seen.first_block, EXCLUDED.first_block),
    first_time  = least(wallet_first_seen.first_time, EXCLUDED.first_time),
    last_block  = greatest(wallet_first_seen.last_block, EXCLUDED.last_block),
    n_swaps     = wallet_first_seen.n_swaps + EXCLUDED.n_swaps,
    updated_at  = now()
"""

def merge(conn, table, lo, hi):
    t = time.time()
    n = conn.execute(MERGE.format(table=table), (lo, hi)).rowcount
    conn.commit(); log(f"{table} blocks {lo+1:,}-{hi:,}: {n:,} wallets merged in {time.time()-t:,.0f}s")

def main():
    if FULL == UPDATE:
        sys.exit(__doc__)
    with psycopg.connect(DB) as conn:
        conn.execute("SET statement_timeout = 0"); conn.execute("SET work_mem = '512MB'")
        if FULL:
            conn.execute("TRUNCATE wallet_first_seen"); conn.commit()
            # the hot table's max block bounds both passes; the archive has no btree on block_number
            # while a backfill is loading, so max() over it would be a full scan
            (hmax,) = conn.execute("SELECT max(block_number) FROM swaps WHERE block_time > now() - interval '1 day'").fetchone()
            hi = min(hmax, MAX_BLOCK) if MAX_BLOCK else hmax
            log(f"full build from swaps_archive and swaps to block {hi:,}")
            merge(conn, "swaps_archive", 0, hi)
            merge(conn, "swaps", 0, hi)
        else:
            (wm,) = conn.execute("SELECT max(last_block) FROM wallet_first_seen").fetchone()
            if wm is None:
                sys.exit("wallet_first_seen is empty: run --full first")
            (hmax,) = conn.execute("SELECT max(block_number) FROM swaps WHERE block_time > now() - interval '1 day'").fetchone()
            if hmax <= wm:
                log("nothing new"); return
            merge(conn, "swaps", wm, hmax)
        (n, lo, hi) = conn.execute("SELECT count(*), min(first_time), max(first_time) FROM wallet_first_seen").fetchone()
        log(f"wallet_first_seen: {n:,} wallets, first swaps {lo:%Y-%m-%d} to {hi:%Y-%m-%d}")

if __name__ == "__main__":
    main()
