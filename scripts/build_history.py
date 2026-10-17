"""Weekly behavioural labels and per-minute aggregates over the swap archive.

For each ISO week (Monday 00:00 UTC) in the range, computes the same wallet features and
labels as build_features.py but from `swaps_archive`, stores them in `wallet_features_hist`
keyed by (wallet, week), then rolls the week up into `pool_minutes_hist` a day at a time.
Progress is kept in `history_weeks`, so it resumes.

Usage: python scripts/build_history.py [--from 2026-06-29] [--to 2026-10-05] [--rollup-only] [--features-only]
Needs the BRIN index on swaps_archive (block_number, block_time); without it every week is a full scan.
"""
import os, sys, time, importlib.util
from datetime import datetime, timedelta, timezone
import psycopg

here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("bf", os.path.join(here, "build_features.py"))
bf = importlib.util.module_from_spec(spec); spec.loader.exec_module(bf)
DB = bf.DB; log = bf.log

def arg(name, default): return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default
FROM = datetime.fromisoformat(arg("--from", "2026-06-29")).replace(tzinfo=timezone.utc)
TO = datetime.fromisoformat(arg("--to", "2026-10-05")).replace(tzinfo=timezone.utc)   # exclusive
ROLLUP_ONLY = "--rollup-only" in sys.argv; FEATURES_ONLY = "--features-only" in sys.argv

HIST = dict(target="wallet_features_hist",
            scope="WHERE window_start = %(week)s::timestamptz",
            scope_f="AND f.window_start = %(week)s::timestamptz",
            scope_and="AND window_start = %(week)s::timestamptz")

SOURCE = """swaps_archive
    WHERE block_number BETWEEN %(lo)s AND %(hi)s AND block_time >= %(week)s::timestamptz AND block_time < %(week_end)s::timestamptz"""

FEATURES_HIST_SQL = (bf.FEATURE_CTES.format(source=SOURCE)
    + "INSERT INTO wallet_features_hist " + bf.FEATURE_COLS
    + bf.FEATURE_SELECT.format(window="%(week)s::timestamptz, %(week_end)s::timestamptz")
    + " ON CONFLICT (wallet, window_start) DO NOTHING")

def session(conn):
    conn.execute("SET work_mem = '512MB'")
    conn.execute("SET temp_tablespaces = 'tethera_archive'")
    conn.execute("SET max_parallel_workers_per_gather = 2")

def block_bounds(conn, a, b):
    lo, hi = conn.execute("SELECT min(block_number), max(block_number) FROM swaps_archive WHERE block_time >= %s AND block_time < %s", (a, b)).fetchone()
    return lo, hi

def main():
    with psycopg.connect(DB) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS history_weeks (
            week_start timestamptz PRIMARY KEY, lo_block bigint, hi_block bigint, wallets integer, automated integer, volume_bots integer,
            features_done timestamptz, rollup_done timestamptz, minutes integer)""")
        conn.commit()
        session(conn)
        # only complete weeks: the archive's newest loaded time, found near the backfill ledger's high-water mark
        cursor = conn.execute("SELECT max(hi) FROM backfill_chunks WHERE hi < 84000000").fetchone()[0]   # the HyperSync backfill's high-water mark
        loaded_to = conn.execute("SELECT max(block_time) FROM swaps_archive WHERE block_number BETWEEN %s - 20000 AND %s", (cursor, cursor)).fetchone()[0]
        log(f"archive loaded through {loaded_to:%Y-%m-%d %H:%M} UTC" if loaded_to else "archive empty")
        week = FROM
        while week < TO:
            week_end = week + timedelta(days=7)
            if loaded_to is None or week_end > loaded_to:
                log(f"week {week:%Y-%m-%d}: not fully loaded yet; stopping"); break
            row = conn.execute("SELECT features_done, rollup_done, lo_block, hi_block FROM history_weeks WHERE week_start = %s", (week,)).fetchone()
            fdone, rdone, lo, hi = row if row else (None, None, None, None)
            if lo is None:
                t = time.time(); lo, hi = block_bounds(conn, week, week_end)
                if lo is None:
                    log(f"week {week:%Y-%m-%d}: no archive rows yet; stopping"); break
                conn.execute("INSERT INTO history_weeks (week_start, lo_block, hi_block) VALUES (%s,%s,%s) ON CONFLICT (week_start) DO UPDATE SET lo_block = EXCLUDED.lo_block, hi_block = EXCLUDED.hi_block", (week, lo, hi))
                conn.commit(); log(f"week {week:%Y-%m-%d}: blocks {lo:,}-{hi:,} ({time.time()-t:.0f}s)")
            params = dict(week=week, week_end=week_end, lo=lo, hi=hi)
            if not fdone and not ROLLUP_ONLY:
                t = time.time()
                n = conn.execute(FEATURES_HIST_SQL, params).rowcount
                conn.execute(bf.LABEL_SQL.format(**HIST), {**bf.cfg, **params})
                conn.execute(bf.VOLUME_BOT_SQL.format(**HIST), {**bf.vb_cfg, **params})
                w, a, v = conn.execute("SELECT count(*), count(*) FILTER (WHERE is_automated), count(*) FILTER (WHERE is_volume_bot) FROM wallet_features_hist WHERE window_start = %s", (week,)).fetchone()
                conn.execute("UPDATE history_weeks SET wallets=%s, automated=%s, volume_bots=%s, features_done=now() WHERE week_start=%s", (w, a, v, week))
                conn.commit()
                log(f"week {week:%Y-%m-%d}: features for {w:,} wallets, {a:,} automated, {v:,} volume bots ({time.time()-t:,.0f}s)")
            if not rdone and not FEATURES_ONLY and (fdone or not ROLLUP_ONLY):
                t = time.time(); total = 0
                day = week
                while day < week_end:
                    nxt = day + timedelta(days=1)
                    dlo, dhi = block_bounds(conn, day, nxt)
                    if dlo is not None:
                        n = conn.execute("SELECT rollup_pool_minutes_hist(%s, %s, %s, %s, %s)", (week, day, nxt, dlo, dhi)).fetchone()[0]
                        total += n; conn.commit()
                        log(f"  {day:%Y-%m-%d}: {n:,} pool-minute rows")
                    day = nxt
                conn.execute("UPDATE history_weeks SET rollup_done=now(), minutes=%s WHERE week_start=%s", (total, week)); conn.commit()
                log(f"week {week:%Y-%m-%d}: rollup {total:,} rows ({time.time()-t:,.0f}s)")
            week = week_end
    log("done")

if __name__ == "__main__":
    main()
