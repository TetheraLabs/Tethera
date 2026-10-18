-- Self-cancelling swaps are rare per pool (a few thousand in the busiest), so a partial index makes "how much of this
-- pool was printed" an index lookup instead of a scan of every swap in the pool.
CREATE INDEX CONCURRENTLY IF NOT EXISTS swaps_self_cancel_pool_idx ON swaps (pool, block_time) WHERE self_cancel;
