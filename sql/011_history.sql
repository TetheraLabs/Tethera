-- Historical labels and aggregates over swaps_archive, computed week by week.
CREATE TABLE IF NOT EXISTS wallet_features_hist (
    wallet              VARCHAR(42) NOT NULL,
    window_start        TIMESTAMPTZ NOT NULL,      -- start of the week the features describe
    window_end          TIMESTAMPTZ NOT NULL,
    n_swaps             INTEGER NOT NULL,
    n_pools             INTEGER NOT NULL,
    span_seconds        DOUBLE PRECISION,
    median_gap          DOUBLE PRECISION,
    min_gap             DOUBLE PRECISION,
    max_gap             DOUBLE PRECISION,
    gap_cv              DOUBLE PRECISION,
    multi_pool_blocks   INTEGER NOT NULL,
    multi_swap_blocks   INTEGER NOT NULL,
    reactive_swaps      INTEGER NOT NULL,
    round_trips         INTEGER NOT NULL,
    round_amount_frac   DOUBLE PRECISION,
    repeat_amount_frac  DOUBLE PRECISION,
    net_flow_frac       DOUBLE PRECISION,
    top_pool_share      DOUBLE PRECISION,
    active_days         INTEGER,
    daily_volume_cv     DOUBLE PRECISION,
    signals             TEXT[] NOT NULL DEFAULT '{}',
    n_signals           INTEGER NOT NULL DEFAULT 0,
    is_automated        BOOLEAN NOT NULL DEFAULT FALSE,
    is_volume_bot       BOOLEAN NOT NULL DEFAULT FALSE,
    is_registered_agent BOOLEAN NOT NULL DEFAULT FALSE,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (wallet, window_start)
);
CREATE INDEX IF NOT EXISTS wallet_features_hist_week_idx ON wallet_features_hist (window_start) WHERE is_automated;

-- Same shape as pool_minutes, filled from the archive with that week's labels. Kept separate so the
-- live table stays small; the view unions them for the outcome study.
CREATE TABLE IF NOT EXISTS pool_minutes_hist (LIKE pool_minutes INCLUDING DEFAULTS) TABLESPACE tethera_archive;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'pool_minutes_hist_pkey') THEN
    ALTER TABLE pool_minutes_hist ADD PRIMARY KEY (pool, minute, direction) USING INDEX TABLESPACE tethera_archive;
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS pool_minutes_hist_minute_idx ON pool_minutes_hist (minute) TABLESPACE tethera_archive;
CREATE OR REPLACE VIEW pool_minutes_all AS SELECT * FROM pool_minutes UNION ALL SELECT * FROM pool_minutes_hist;

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
    AND s.block_time >= from_ts AND s.block_time < to_ts
  GROUP BY 1,2,3
  ON CONFLICT (pool, minute, direction) DO UPDATE SET
    swaps = EXCLUDED.swaps, wallets = EXCLUDED.wallets, auto_swaps = EXCLUDED.auto_swaps, auto_wallets = EXCLUDED.auto_wallets,
    amount0_in = EXCLUDED.amount0_in, amount1_in = EXCLUDED.amount1_in, px_median = EXCLUDED.px_median,
    px_first = EXCLUDED.px_first, px_last = EXCLUDED.px_last, first_block = EXCLUDED.first_block, last_block = EXCLUDED.last_block;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$ LANGUAGE plpgsql;
