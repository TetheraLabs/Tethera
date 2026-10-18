-- The hot table is append-only in block order, so a BRIN on (block_number, block_time) answers "the last N minutes"
-- without scanning all 18M rows (there was no time index at all; every ten-minute run paid a full scan for it).
CREATE INDEX CONCURRENTLY IF NOT EXISTS swaps_time_brin ON swaps USING brin (block_number, block_time) WITH (pages_per_range = 64);
