-- Progress ledger for the parallel backfill. One row per finished 2,000-block chunk, so workers
-- can complete chunks out of order and a restart re-queues only the chunks that never finished.
CREATE TABLE IF NOT EXISTS backfill_chunks (
  lo        bigint PRIMARY KEY,
  hi        bigint NOT NULL,
  swaps     integer NOT NULL,
  seconds   real,
  done_at   timestamptz NOT NULL DEFAULT now()
);
