-- Per pool, per minute, per direction rollup. This is what the signal, the crowding queries and the
-- outcome study read, and what survives after raw swaps age out. A few hundred MB per month.
CREATE TABLE IF NOT EXISTS pool_minutes (
    pool              VARCHAR(66) NOT NULL,
    minute            TIMESTAMPTZ NOT NULL,
    direction         VARCHAR(5)  NOT NULL,
    swaps             INTEGER NOT NULL,
    wallets           INTEGER NOT NULL,
    auto_swaps        INTEGER NOT NULL,
    auto_wallets      INTEGER NOT NULL,
    amount0_in        NUMERIC(78,0) NOT NULL,     -- sum of |amount0| for this direction
    amount1_in        NUMERIC(78,0) NOT NULL,
    px_median         NUMERIC,                    -- median |amount1|/|amount0|
    px_first          NUMERIC,
    px_last           NUMERIC,
    first_block       BIGINT NOT NULL,
    last_block        BIGINT NOT NULL,
    PRIMARY KEY (pool, minute, direction)
);
CREATE INDEX IF NOT EXISTS pool_minutes_minute_idx ON pool_minutes (minute);

-- Rollup for a time range; idempotent (upserts). Labels are whatever swaps.is_automated says at rollup time.
CREATE OR REPLACE FUNCTION rollup_pool_minutes(from_ts TIMESTAMPTZ, to_ts TIMESTAMPTZ) RETURNS INTEGER AS $$
DECLARE n INTEGER;
BEGIN
  INSERT INTO pool_minutes
  SELECT pool, date_trunc('minute', block_time) AS minute, direction,
         count(*), count(DISTINCT tx_from),
         count(*) FILTER (WHERE is_automated), count(DISTINCT tx_from) FILTER (WHERE is_automated),
         sum(abs(amount0)), sum(abs(amount1)),
         percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(amount1)::numeric / nullif(abs(amount0),0)),
         (array_agg(abs(amount1)::numeric / nullif(abs(amount0),0) ORDER BY block_number, log_index))[1],
         (array_agg(abs(amount1)::numeric / nullif(abs(amount0),0) ORDER BY block_number DESC, log_index DESC))[1],
         min(block_number), max(block_number)
  FROM swaps
  WHERE block_time >= from_ts AND block_time < to_ts
  GROUP BY 1,2,3
  ON CONFLICT (pool, minute, direction) DO UPDATE SET
    swaps = EXCLUDED.swaps, wallets = EXCLUDED.wallets, auto_swaps = EXCLUDED.auto_swaps, auto_wallets = EXCLUDED.auto_wallets,
    amount0_in = EXCLUDED.amount0_in, amount1_in = EXCLUDED.amount1_in, px_median = EXCLUDED.px_median,
    px_first = EXCLUDED.px_first, px_last = EXCLUDED.px_last, first_block = EXCLUDED.first_block, last_block = EXCLUDED.last_block;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$ LANGUAGE plpgsql;
