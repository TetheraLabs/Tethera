-- First and last swap of every wallet, over the archive and the hot table together.
-- Built once from swaps_archive (scripts/wallet_first_seen.py --full) and advanced nightly from the
-- hot table (--update). A wallet whose first swap is inside a token's launch window is fresh for
-- that launch wherever on the chain it traded first, not only where our hot table starts.
CREATE TABLE IF NOT EXISTS wallet_first_seen (
    wallet      VARCHAR(42) PRIMARY KEY,
    first_block BIGINT NOT NULL,
    first_time  TIMESTAMPTZ NOT NULL,
    last_block  BIGINT NOT NULL,
    n_swaps     BIGINT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS wallet_first_seen_time_idx ON wallet_first_seen (first_time);
