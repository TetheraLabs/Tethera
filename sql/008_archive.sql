-- Cold archive of historical swaps on the external drive (tablespace tethera_archive).
-- swaps      = hot tail on the server's SSD (last 14 days), serves the live signal API.
-- swaps_archive = everything older, plus the historical backfill. Same columns, fewer indexes.
CREATE TABLESPACE tethera_archive OWNER user LOCATION '/path/to/archive/pg/tethera_archive';

CREATE TABLE swaps_archive (LIKE swaps INCLUDING DEFAULTS INCLUDING CONSTRAINTS) TABLESPACE tethera_archive;
ALTER TABLE swaps_archive ADD PRIMARY KEY (tx_hash, log_index) USING INDEX TABLESPACE tethera_archive;
CREATE INDEX swaps_archive_pool_time_idx ON swaps_archive (pool, block_time) TABLESPACE tethera_archive;
CREATE INDEX swaps_archive_from_idx ON swaps_archive (tx_from) TABLESPACE tethera_archive;
CREATE INDEX swaps_archive_block_idx ON swaps_archive (block_number) TABLESPACE tethera_archive;

-- Both halves as one relation, for historical analysis.
CREATE VIEW swaps_all AS SELECT * FROM swaps UNION ALL SELECT * FROM swaps_archive;

-- Nightly: move rows older than keep_days from the hot table to the archive. Returns rows moved.
CREATE OR REPLACE FUNCTION archive_old_swaps(keep_days int DEFAULT 14) RETURNS bigint AS $$
DECLARE moved bigint;
BEGIN
  WITH old AS (
    DELETE FROM swaps WHERE block_time < now() - make_interval(days => keep_days) RETURNING *
  ), ins AS (
    INSERT INTO swaps_archive SELECT * FROM old ON CONFLICT (tx_hash, log_index) DO NOTHING RETURNING 1
  )
  SELECT count(*) INTO moved FROM ins;
  RETURN moved;
END $$ LANGUAGE plpgsql;
