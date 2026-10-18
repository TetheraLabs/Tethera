-- Self-cancelling swaps: one transaction swapping both ways in one pool with both token amounts netting to (almost)
-- nothing. Under Uniswap v4 flash accounting such a trip settles nothing, so in a zero-fee pool it prints volume for
-- the gas alone. In the week to 18 Oct 2026 one wallet printed $1.0B this way through 26 pools it had seeded itself.
-- Flagged swaps stay in the tables (the count is a product in itself) but every aggregate, label and feed ignores them.
ALTER TABLE swaps         ADD COLUMN IF NOT EXISTS self_cancel BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE swaps_archive ADD COLUMN IF NOT EXISTS self_cancel BOOLEAN NOT NULL DEFAULT FALSE;
DROP VIEW IF EXISTS swaps_all; CREATE VIEW swaps_all AS SELECT * FROM swaps UNION ALL SELECT * FROM swaps_archive;

-- Flag (or unflag) every swap in a block range of one table. Net under 1% of gross in both tokens. Idempotent.
CREATE OR REPLACE FUNCTION flag_self_cancelling(tbl TEXT, from_block BIGINT, to_block BIGINT) RETURNS INTEGER AS $$
DECLARE n INTEGER;
BEGIN
  EXECUTE format($q$
    WITH g AS (
      SELECT tx_hash, pool,
             count(DISTINCT direction) = 2
             AND abs(sum(amount0)) < 0.01 * sum(abs(amount0))
             AND abs(sum(amount1)) < 0.01 * sum(abs(amount1)) AS sc
      FROM %I WHERE block_number BETWEEN $1 AND $2
      GROUP BY 1, 2 HAVING count(*) > 1
    )
    UPDATE %I s SET self_cancel = g.sc FROM g
    WHERE s.tx_hash = g.tx_hash AND s.pool = g.pool AND s.block_number BETWEEN $1 AND $2 AND s.self_cancel IS DISTINCT FROM g.sc
  $q$, tbl, tbl) USING from_block, to_block;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$ LANGUAGE plpgsql;

-- Per-minute aggregates ignore self-cancelling swaps (live and history).
CREATE OR REPLACE FUNCTION rollup_pool_minutes(from_ts TIMESTAMPTZ, to_ts TIMESTAMPTZ) RETURNS INTEGER AS $$
DECLARE n INTEGER;
BEGIN
  DELETE FROM pool_minutes WHERE minute >= date_trunc('minute', from_ts) AND minute < to_ts;
  INSERT INTO pool_minutes
  SELECT s.pool, date_trunc('minute', s.block_time) AS minute, s.direction,
         count(*), count(DISTINCT s.tx_from),
         count(*) FILTER (WHERE s.is_automated AND NOT coalesce(f.is_volume_bot, false)),
         count(DISTINCT s.tx_from) FILTER (WHERE s.is_automated AND NOT coalesce(f.is_volume_bot, false)),
         sum(abs(s.amount0)), sum(abs(s.amount1)),
         percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(s.amount1)::numeric / nullif(abs(s.amount0),0)),
         (array_agg(abs(s.amount1)::numeric / nullif(abs(s.amount0),0) ORDER BY s.block_number, s.log_index))[1],
         (array_agg(abs(s.amount1)::numeric / nullif(abs(s.amount0),0) ORDER BY s.block_number DESC, s.log_index DESC))[1],
         min(s.block_number), max(s.block_number)
  FROM swaps s LEFT JOIN wallet_features f ON f.wallet = s.tx_from AND f.is_volume_bot
  WHERE s.block_time >= from_ts AND s.block_time < to_ts AND NOT s.self_cancel
  GROUP BY 1,2,3
  ON CONFLICT (pool, minute, direction) DO UPDATE SET
    swaps = EXCLUDED.swaps, wallets = EXCLUDED.wallets, auto_swaps = EXCLUDED.auto_swaps, auto_wallets = EXCLUDED.auto_wallets,
    amount0_in = EXCLUDED.amount0_in, amount1_in = EXCLUDED.amount1_in, px_median = EXCLUDED.px_median,
    px_first = EXCLUDED.px_first, px_last = EXCLUDED.px_last, first_block = EXCLUDED.first_block, last_block = EXCLUDED.last_block;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION rollup_pool_minutes_hist(week_start TIMESTAMPTZ, from_ts TIMESTAMPTZ, to_ts TIMESTAMPTZ,
                                                    from_block BIGINT, to_block BIGINT) RETURNS INTEGER AS $$
DECLARE n INTEGER;
BEGIN
  INSERT INTO pool_minutes_hist
  SELECT s.pool, date_trunc('minute', s.block_time) AS minute, s.direction,
         count(*), count(DISTINCT s.tx_from),
         count(*) FILTER (WHERE f.is_automated AND NOT f.is_volume_bot),
         count(DISTINCT s.tx_from) FILTER (WHERE f.is_automated AND NOT f.is_volume_bot),
         sum(abs(s.amount0)), sum(abs(s.amount1)),
         percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(s.amount1)::numeric / nullif(abs(s.amount0),0)),
         (array_agg(abs(s.amount1)::numeric / nullif(abs(s.amount0),0) ORDER BY s.block_number, s.log_index))[1],
         (array_agg(abs(s.amount1)::numeric / nullif(abs(s.amount0),0) ORDER BY s.block_number DESC, s.log_index DESC))[1],
         min(s.block_number), max(s.block_number)
  FROM swaps_archive s
  LEFT JOIN wallet_features_hist f ON f.wallet = s.tx_from AND f.window_start = week_start
  WHERE s.block_number >= from_block AND s.block_number <= to_block     -- BRIN-prunable
    AND s.block_time >= from_ts AND s.block_time < to_ts AND NOT s.self_cancel
  GROUP BY 1,2,3
  ON CONFLICT (pool, minute, direction) DO UPDATE SET
    swaps = EXCLUDED.swaps, wallets = EXCLUDED.wallets, auto_swaps = EXCLUDED.auto_swaps, auto_wallets = EXCLUDED.auto_wallets,
    amount0_in = EXCLUDED.amount0_in, amount1_in = EXCLUDED.amount1_in, px_median = EXCLUDED.px_median,
    px_first = EXCLUDED.px_first, px_last = EXCLUDED.px_last, first_block = EXCLUDED.first_block, last_block = EXCLUDED.last_block;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$ LANGUAGE plpgsql;
ALTER FUNCTION rollup_pool_minutes_hist(timestamptz,timestamptz,timestamptz,bigint,bigint) SET enable_nestloop = off;
