-- The archive's identity becomes (block_number, log_index): a unique index whose new keys always land at its end, so
-- the nightly move appends instead of making one random lookup per row in a 37 GB (tx_hash, log_index) key on the
-- external drive. The index was built beforehand (CREATE UNIQUE INDEX swaps_archive_block_log_key ON swaps_archive
-- (block_number, log_index) TABLESPACE tethera_archive); this swaps the move over to it and drops the old key.
BEGIN;
SET LOCAL lock_timeout = '30s';
CREATE OR REPLACE FUNCTION archive_swaps_day(day_start timestamptz) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE moved bigint;
BEGIN
  WITH old AS (
    DELETE FROM swaps WHERE block_time >= day_start AND block_time < day_start + interval '1 day' RETURNING *
  ), ins AS (
    INSERT INTO swaps_archive SELECT * FROM old ON CONFLICT (block_number, log_index) DO NOTHING RETURNING 1
  )
  SELECT count(*) INTO moved FROM ins;
  RETURN moved;
END $$;
ALTER TABLE swaps_archive DROP CONSTRAINT swaps_archive_pkey;
COMMIT;
