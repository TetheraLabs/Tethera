"""Run the week-2 analyses as soon as their inputs exist, then stop.

Waits (polling every 10 minutes) until build_history.py has finished every week from 29 June to
17 August, then runs the outcome study over 1 July to 17 August. Separately, once the archive's
(pool, block_time) index exists (built by backfill_hypersync.py --finish after the full load),
runs the lead-time benchmark on the archive with longer horizons. Logs to data/run_week2.log.

Usage: nohup python scripts/run_week2.py &
"""
import os, sys, time, subprocess
from datetime import datetime, timezone
import psycopg

DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
HERE = os.path.dirname(os.path.abspath(__file__)); PY = sys.executable
log = lambda *a: print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)
WEEKS_NEEDED = 7          # 29 Jun .. 10 Aug starts, i.e. through 17 Aug
STUDY_FROM, STUDY_TO = "2026-07-01", "2026-08-17"

def weeks_done(conn):
    return conn.execute("SELECT count(*) FROM history_weeks WHERE week_start >= '2026-06-29' AND week_start < '2026-08-17' AND rollup_done IS NOT NULL").fetchone()[0]

def archive_indexed(conn):
    return conn.execute("SELECT count(*) FROM pg_indexes WHERE indexname = 'swaps_archive_pool_time_idx'").fetchone()[0] == 1

def run(label, args):
    log(f"starting {label}: {' '.join(args)}")
    t = time.time(); r = subprocess.run([PY, "-u"] + args, cwd=os.path.dirname(HERE), capture_output=True, text=True)
    log(f"{label} exit {r.returncode} in {time.time()-t:,.0f}s"); log(r.stdout[-3000:]); 
    if r.returncode: log(r.stderr[-3000:])
    return r.returncode == 0

study_done = bench_done = False
while not (study_done and bench_done):
    try:
        with psycopg.connect(DB) as conn:
            n = weeks_done(conn); idx = archive_indexed(conn)
        if not study_done:
            if n >= WEEKS_NEEDED:
                study_done = run("outcome study", [os.path.join(HERE, "outcome_study.py"), "--from", STUDY_FROM, "--to", STUDY_TO,
                                                   "--out", os.path.join(os.path.dirname(HERE), "reports", "outcome_study.md")])
            else:
                log(f"history: {n}/{WEEKS_NEEDED} weeks rolled up; waiting")
        if not bench_done:
            if idx:
                bench_done = run("lead-time benchmark (archive)", [os.path.join(HERE, "lead_time_benchmark.py"), "--table", "swaps_archive",
                                                                   "--from", "2026-08-01", "--to", "2026-08-17",
                                                                   "--out", os.path.join(os.path.dirname(HERE), "reports", "lead_time_benchmark_archive.md")])
            else:
                log("archive pool index not built yet; benchmark waits")
    except Exception as e:
        log(f"error: {type(e).__name__}: {str(e)[:200]}")
    if not (study_done and bench_done):
        time.sleep(600)
log("week-2 pipeline complete")
