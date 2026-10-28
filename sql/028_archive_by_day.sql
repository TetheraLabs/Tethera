-- Move one day of old swaps at a time, so the nightly archive holds locks briefly and a failure costs one day, not the run.
CREATE OR REPLACE FUNCTION archive_swaps_day(day_start timestamptz) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE moved bigint;
BEGIN
  WITH old AS (
    DELETE FROM swaps WHERE block_time >= day_start AND block_time < day_start + interval '1 day' RETURNING *
  ), ins AS (
    INSERT INTO swaps_archive SELECT * FROM old ON CONFLICT (tx_hash, log_index) DO NOTHING RETURNING 1
  )
  SELECT count(*) INTO moved FROM ins;
  RETURN moved;
END $$;
