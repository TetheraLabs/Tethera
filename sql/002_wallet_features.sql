-- Per-wallet behavioural features over a time window, and the automated label derived from them.

CREATE TABLE IF NOT EXISTS wallet_features (
    wallet              VARCHAR(42) PRIMARY KEY,
    window_start        TIMESTAMPTZ NOT NULL,
    window_end          TIMESTAMPTZ NOT NULL,
    n_swaps             INTEGER NOT NULL,
    n_pools             INTEGER NOT NULL,
    span_seconds        DOUBLE PRECISION,       -- first swap to last swap
    median_gap          DOUBLE PRECISION,       -- seconds between consecutive swaps
    min_gap             DOUBLE PRECISION,
    max_gap             DOUBLE PRECISION,       -- longest idle stretch inside the span
    gap_cv              DOUBLE PRECISION,       -- stddev/mean of gaps; low = metronomic
    multi_pool_blocks   INTEGER NOT NULL,       -- blocks where it swapped in >1 pool (atomic arb)
    multi_swap_blocks   INTEGER NOT NULL,       -- blocks where it swapped >1 time
    reactive_swaps      INTEGER NOT NULL,       -- swaps within 2 blocks after someone else's swap in the same pool
    round_trips         INTEGER NOT NULL,       -- opposite-direction swap in same pool within 30 s
    round_amount_frac   DOUBLE PRECISION,       -- share of input amounts with <=3 significant digits
    repeat_amount_frac  DOUBLE PRECISION,       -- 1 - distinct input amounts / swaps
    lifetime_tx_count   BIGINT,                 -- nonce, filled by a separate lookup
    is_contract         BOOLEAN,
    signals             TEXT[] NOT NULL DEFAULT '{}',
    n_signals           INTEGER NOT NULL DEFAULT 0,
    is_automated        BOOLEAN NOT NULL DEFAULT FALSE,
    is_registered_agent BOOLEAN NOT NULL DEFAULT FALSE,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE swaps ADD COLUMN IF NOT EXISTS is_automated BOOLEAN;
CREATE INDEX IF NOT EXISTS swaps_pool_block_idx ON swaps (pool, block_number);
CREATE INDEX IF NOT EXISTS swaps_automated_idx ON swaps (is_automated) WHERE is_automated;
