-- Volume bots: wallets running paid volume services (fixed daily volume, self-trading round trips,
-- one pool, no net position). They are automated but they are not a herd, so herd scoring excludes them.
ALTER TABLE wallet_features ADD COLUMN IF NOT EXISTS net_flow_frac     DOUBLE PRECISION;  -- sum over pools |net token0| / gross token0; ~0 = self-trading
ALTER TABLE wallet_features ADD COLUMN IF NOT EXISTS top_pool_share    DOUBLE PRECISION;  -- share of its swaps in its busiest pool
ALTER TABLE wallet_features ADD COLUMN IF NOT EXISTS active_days       INTEGER;
ALTER TABLE wallet_features ADD COLUMN IF NOT EXISTS daily_volume_cv   DOUBLE PRECISION;  -- stddev/mean of daily gross volume; low = fixed budget
ALTER TABLE wallet_features ADD COLUMN IF NOT EXISTS is_volume_bot     BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS wallet_features_volume_bot_idx ON wallet_features (wallet) WHERE is_volume_bot;

-- Per-minute aggregates: automated counts exclude volume bots.
CREATE OR REPLACE FUNCTION rollup_pool_minutes(from_ts TIMESTAMPTZ, to_ts TIMESTAMPTZ) RETURNS INTEGER AS $$
DECLARE n INTEGER;
BEGIN
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
  WHERE s.block_time >= from_ts AND s.block_time < to_ts
  GROUP BY 1,2,3
  ON CONFLICT (pool, minute, direction) DO UPDATE SET
    swaps = EXCLUDED.swaps, wallets = EXCLUDED.wallets, auto_swaps = EXCLUDED.auto_swaps, auto_wallets = EXCLUDED.auto_wallets,
    amount0_in = EXCLUDED.amount0_in, amount1_in = EXCLUDED.amount1_in, px_median = EXCLUDED.px_median,
    px_first = EXCLUDED.px_first, px_last = EXCLUDED.px_last, first_block = EXCLUDED.first_block, last_block = EXCLUDED.last_block;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$ LANGUAGE plpgsql;
