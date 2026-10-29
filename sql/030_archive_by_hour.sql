-- The nightly move, an hour at a time. The live table's BRIN on block_time stopped narrowing anything once the labelling
-- job's updates had scattered rows across pages (one hour's delete read 1.39M of 1.6M heap pages), so a plain b-tree on
-- block_time finds the rows; each hour commits on its own so row locks last minutes, not hours; rows are inserted in
-- tx_from order so the archive's wallet index is written in key order.
CREATE INDEX CONCURRENTLY IF NOT EXISTS swaps_block_time_idx ON swaps (block_time);

CREATE OR REPLACE FUNCTION archive_swaps_range(a timestamptz, b timestamptz) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE moved bigint;
BEGIN
  WITH old AS (
    DELETE FROM swaps WHERE block_time >= a AND block_time < b RETURNING *
  ), ins AS (
    INSERT INTO swaps_archive SELECT * FROM old ORDER BY tx_from ON CONFLICT (block_number, log_index) DO NOTHING RETURNING 1
  )
  SELECT count(*) INTO moved FROM ins;
  RETURN moved;
END $$;

CREATE OR REPLACE FUNCTION archive_swaps_day(day_start timestamptz) RETURNS bigint LANGUAGE sql AS $$
  SELECT archive_swaps_range(day_start, day_start + interval '1 day')
$$;
