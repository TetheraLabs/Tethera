"""Compute per-wallet behavioural features from the swaps table and label automated wallets.

Usage: python scripts/build_features.py [--since HOURS]   (default: everything in the table)
"""
import os, sys, time, tomllib
import psycopg

DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
_cfg_all = tomllib.load(open(os.path.join(os.path.dirname(__file__), "..", "config", "labels.toml"), "rb"))
cfg = _cfg_all["signals"]; vb_cfg = _cfg_all["volume_bots"]
since = float(sys.argv[sys.argv.index("--since") + 1]) if "--since" in sys.argv else None
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)

FEATURE_CTES = """
WITH s AS (
    SELECT tx_from AS w, pool, block_number, log_index, block_time, direction,
           extract(epoch FROM block_time) AS t,
           CASE WHEN amount0 > 0 THEN amount0 ELSE amount1 END AS amount_in,
           amount0
    FROM {source}
),
ordered AS (
    SELECT *, t - lag(t) OVER (PARTITION BY w ORDER BY block_number, log_index) AS gap
    FROM s
),
per_wallet AS (
    SELECT w, count(*) AS n_swaps, count(DISTINCT pool) AS n_pools,
           min(block_time) AS first_t, max(block_time) AS last_t,
           extract(epoch FROM max(block_time) - min(block_time)) AS span_seconds
    FROM s GROUP BY w
),
gapstats AS (
    SELECT w,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY gap) AS median_gap,
           min(gap) AS min_gap, max(gap) AS max_gap,
           stddev_samp(gap) / nullif(avg(gap), 0) AS gap_cv
    FROM ordered WHERE gap IS NOT NULL GROUP BY w
),
blocks AS (
    SELECT w,
           count(*) FILTER (WHERE npools > 1) AS multi_pool_blocks,
           count(*) FILTER (WHERE nswaps > 1) AS multi_swap_blocks
    FROM (SELECT w, block_number, count(DISTINCT pool) AS npools, count(*) AS nswaps FROM s GROUP BY 1, 2) b
    GROUP BY w
),
reaction AS (
    SELECT a.w, count(DISTINCT (a.block_number, a.log_index)) AS reactive_swaps
    FROM s a
    WHERE EXISTS (SELECT 1 FROM s b WHERE b.pool = a.pool AND b.w <> a.w
                  AND b.block_number BETWEEN a.block_number - 2 AND a.block_number - 1)
    GROUP BY a.w
),
roundtrip AS (
    SELECT w, count(*) AS round_trips FROM (
        SELECT w, pool, direction, t,
               lag(direction) OVER (PARTITION BY w, pool ORDER BY block_number, log_index) AS pd,
               lag(t)         OVER (PARTITION BY w, pool ORDER BY block_number, log_index) AS pt
        FROM s) x
    WHERE pd IS NOT NULL AND pd <> direction AND t - pt <= 30
    GROUP BY w
),
netflow AS (
    SELECT w, sum(abs(net0))::float / nullif(sum(gross0), 0) AS net_flow_frac,
           max(cnt)::float / sum(cnt) AS top_pool_share
    FROM (SELECT w, pool, sum(amount0) AS net0, sum(abs(amount0)) AS gross0, count(*) AS cnt FROM s GROUP BY 1, 2) x
    GROUP BY w
),
daily AS (
    SELECT w, count(*) AS active_days, stddev_samp(v) / nullif(avg(v), 0) AS daily_volume_cv
    FROM (SELECT w, date_trunc('day', block_time) AS d, sum(abs(amount0)) AS v FROM s GROUP BY 1, 2) x
    GROUP BY w
),
sizing AS (
    SELECT w,
           avg(CASE WHEN amount_in > 0 AND mod(amount_in, power(10::numeric, greatest(length(amount_in::text) - 3, 0))) = 0 THEN 1.0 ELSE 0.0 END) AS round_amount_frac,
           1.0 - count(DISTINCT amount_in)::float / count(*) AS repeat_amount_frac
    FROM s GROUP BY w
)
"""

FEATURE_COLS = """(wallet, window_start, window_end, n_swaps, n_pools, span_seconds,
    median_gap, min_gap, max_gap, gap_cv, multi_pool_blocks, multi_swap_blocks, reactive_swaps, round_trips,
    round_amount_frac, repeat_amount_frac, is_registered_agent, net_flow_frac, top_pool_share, active_days, daily_volume_cv)"""

FEATURE_SELECT = """
SELECT p.w, {window}, p.n_swaps, p.n_pools, p.span_seconds,
       g.median_gap, g.min_gap, g.max_gap, g.gap_cv,
       coalesce(b.multi_pool_blocks, 0), coalesce(b.multi_swap_blocks, 0),
       coalesce(r.reactive_swaps, 0), coalesce(rt.round_trips, 0),
       z.round_amount_frac, z.repeat_amount_frac,
       EXISTS (SELECT 1 FROM agents a WHERE a.agent_wallet = p.w OR a.owner = p.w OR a.current_owner = p.w),
       nf.net_flow_frac, nf.top_pool_share, d.active_days, d.daily_volume_cv
FROM per_wallet p
LEFT JOIN gapstats g USING (w) LEFT JOIN blocks b USING (w) LEFT JOIN reaction r USING (w)
LEFT JOIN roundtrip rt USING (w) LEFT JOIN sizing z USING (w) LEFT JOIN netflow nf USING (w) LEFT JOIN daily d USING (w)
"""

# The reaction signal has two implementations with the same meaning (a swap that follows another
# wallet's swap in the same pool one or two blocks earlier). The EXISTS form is exact but quadratic
# within a pool, fine for the ten-minute run (6 h window) and hopeless for a week; the window form
# looks back at the three preceding swaps in the pool and is what the history build uses.
REACTION_EXISTS = FEATURE_CTES[FEATURE_CTES.index("reaction AS ("):FEATURE_CTES.index("roundtrip AS (")]
REACTION_WINDOW = """reaction AS (
    SELECT w, count(*) AS reactive_swaps FROM (
        SELECT w, block_number,
               lag(w, 1) OVER o AS w1, lag(block_number, 1) OVER o AS b1,
               lag(w, 2) OVER o AS w2, lag(block_number, 2) OVER o AS b2,
               lag(w, 3) OVER o AS w3, lag(block_number, 3) OVER o AS b3
        FROM s WINDOW o AS (PARTITION BY pool ORDER BY block_number, log_index)) x
    WHERE (w1 <> w AND b1 BETWEEN block_number - 2 AND block_number - 1)
       OR (w2 <> w AND b2 BETWEEN block_number - 2 AND block_number - 1)
       OR (w3 <> w AND b3 BETWEEN block_number - 2 AND block_number - 1)
    GROUP BY w
),
"""
FEATURE_CTES_WINDOW = FEATURE_CTES.replace(REACTION_EXISTS, REACTION_WINDOW)
WINDOW_REACTION_FROM_HOURS = 24   # windows this long or longer use the window form

def features_sql(ctes):
  return (ctes.format(source="swaps WHERE (%(since)s::timestamptz IS NULL OR block_time >= %(since)s::timestamptz)")
    + "INSERT INTO wallet_features " + FEATURE_COLS + FEATURE_SELECT.format(window="p.first_t, p.last_t") + """
ON CONFLICT (wallet) DO UPDATE SET
    window_start = EXCLUDED.window_start, window_end = EXCLUDED.window_end, n_swaps = EXCLUDED.n_swaps,
    n_pools = EXCLUDED.n_pools, span_seconds = EXCLUDED.span_seconds, median_gap = EXCLUDED.median_gap,
    min_gap = EXCLUDED.min_gap, max_gap = EXCLUDED.max_gap, gap_cv = EXCLUDED.gap_cv,
    multi_pool_blocks = EXCLUDED.multi_pool_blocks, multi_swap_blocks = EXCLUDED.multi_swap_blocks,
    reactive_swaps = EXCLUDED.reactive_swaps, round_trips = EXCLUDED.round_trips,
    round_amount_frac = EXCLUDED.round_amount_frac, repeat_amount_frac = EXCLUDED.repeat_amount_frac,
    is_registered_agent = EXCLUDED.is_registered_agent, net_flow_frac = EXCLUDED.net_flow_frac,
    top_pool_share = EXCLUDED.top_pool_share,
    -- the multi-day fields are only trusted from a window of three days or more (the weekly run);
    -- the ten-minute run keeps whatever the weekly run last said
    active_days = CASE WHEN EXCLUDED.active_days >= 3 OR wallet_features.active_days IS NULL THEN EXCLUDED.active_days ELSE wallet_features.active_days END,
    daily_volume_cv = CASE WHEN EXCLUDED.active_days >= 3 OR wallet_features.active_days IS NULL THEN EXCLUDED.daily_volume_cv ELSE wallet_features.daily_volume_cv END,
    computed_at = now();
""")

FEATURES_SQL = features_sql(FEATURE_CTES)
FEATURES_SQL_WINDOW = features_sql(FEATURE_CTES_WINDOW)

VOLUME_BOT_SQL = """
UPDATE {target} SET is_volume_bot = coalesce(
    n_swaps >= %(min_swaps)s AND net_flow_frac <= %(net_flow_frac_max)s AND top_pool_share >= %(top_pool_share_min)s
    AND (active_days < 3 OR daily_volume_cv <= %(daily_volume_cv_max)s), false)
WHERE is_volume_bot IS DISTINCT FROM coalesce(
    n_swaps >= %(min_swaps)s AND net_flow_frac <= %(net_flow_frac_max)s AND top_pool_share >= %(top_pool_share_min)s
    AND (active_days < 3 OR daily_volume_cv <= %(daily_volume_cv_max)s), false) {scope_and};
"""

LIVE = dict(target="wallet_features", scope="", scope_f="", scope_and="")

LABEL_SQL = """
UPDATE {target} f SET signals = sig.signals, n_signals = cardinality(sig.signals),
    is_automated = (f.n_swaps >= %(min_swaps_to_label)s AND cardinality(sig.signals) >= %(min_signals)s)
FROM (
    SELECT wallet, array_remove(ARRAY[
        CASE WHEN multi_pool_blocks >= %(multi_pool_block_min)s THEN 'same_block_multi_pool' END,
        CASE WHEN min_gap IS NOT NULL AND min_gap <= %(sub_second_gap_max)s THEN 'sub_second_gap' END,
        CASE WHEN n_swaps >= 10 AND gap_cv IS NOT NULL AND gap_cv < %(metronomic_cv_max)s THEN 'metronomic' END,
        CASE WHEN round_trips >= %(round_trips_min)s THEN 'fast_round_trips' END,
        CASE WHEN n_swaps >= 10 AND round_amount_frac < %(computed_sizes_max_round_frac)s THEN 'computed_sizes' END,
        CASE WHEN n_swaps >= 10 AND reactive_swaps::float / n_swaps >= %(reactive_frac_min)s THEN 'reactive' END,
        CASE WHEN n_swaps >= 20 AND span_seconds >= 1800 AND max_gap <= %(persistent_max_idle)s THEN 'persistent' END
    ], NULL) AS signals
    FROM {target} {scope}
) sig WHERE sig.wallet = f.wallet {scope_f};
"""

def main():
  with psycopg.connect(DB) as conn, conn.cursor() as cur:
      t = time.time()
      cur.execute("SELECT max(block_time) FROM swaps"); end = cur.fetchone()[0]
      since_ts = None if since is None or end is None else end - __import__("datetime").timedelta(hours=since)
      for attempt in range(5):
          try:
              cur.execute(FEATURES_SQL if since is not None and since < WINDOW_REACTION_FROM_HOURS else FEATURES_SQL_WINDOW, {"since": since_ts}); n = cur.rowcount
              conn.commit(); break
          except psycopg.errors.DeadlockDetected:
              conn.rollback(); log(f"deadlock, retry {attempt+1}"); time.sleep(5)
      log(f"features computed for {n} wallets in {time.time()-t:.1f}s ({'exists' if since is not None and since < WINDOW_REACTION_FROM_HOURS else 'window'} reaction, since={since})")
      cur.execute(LABEL_SQL.format(**LIVE), cfg); conn.commit()
      cur.execute(VOLUME_BOT_SQL.format(**LIVE), vb_cfg); conn.commit()
      cur.execute("UPDATE swaps s SET is_automated = f.is_automated FROM wallet_features f WHERE s.tx_from = f.wallet AND s.is_automated IS DISTINCT FROM f.is_automated")
      conn.commit()
      cur.execute("""SELECT count(*), count(*) FILTER (WHERE is_automated), count(*) FILTER (WHERE n_swaps >= %s),
                            count(*) FILTER (WHERE is_registered_agent) FROM wallet_features""", (cfg["min_swaps_to_label"],))
      total, auto, eligible, reg = cur.fetchone()
      log(f"wallets: {total} seen, {eligible} with enough swaps to judge, {auto} labelled automated, {reg} registered agents")
      cur.execute("SELECT count(*), count(*) FILTER (WHERE is_automated), coalesce(sum(n_swaps), 0) FROM wallet_features WHERE is_volume_bot")
      vb, vb_auto, vb_swaps = cur.fetchone()
      log(f"volume bots: {vb} wallets ({vb_auto} of them also labelled automated), {vb_swaps} swaps; excluded from herd counts")
      cur.execute("SELECT count(*), count(*) FILTER (WHERE is_automated) FROM swaps"); ts, ta = cur.fetchone()
      log(f"swaps: {ts} total, {ta} by automated wallets ({ta/max(ts,1):.0%})")
      cur.execute("SELECT unnest(signals) s, count(*) FROM wallet_features GROUP BY 1 ORDER BY 2 DESC")
      log("signal counts:", dict(cur.fetchall()))
      cur.execute("SELECT n_signals, count(*) FROM wallet_features WHERE n_swaps >= %s GROUP BY 1 ORDER BY 1", (cfg["min_swaps_to_label"],))
      log("signals per judged wallet:", dict(cur.fetchall()))

      # keep the per-minute aggregates current (last 30 minutes, idempotent)
      cur.execute("SELECT rollup_pool_minutes(now() - interval '30 minutes', now())"); conn.commit()
      log(f"pool_minutes rolled up: {cur.fetchone()[0]} rows")

if __name__ == "__main__":
    main()
