-- Wallet fingerprints: a behavioural profile per automated or registered wallet over the last week, and
-- the archetypes (k-means clusters) those profiles fall into. Written nightly by scripts/fingerprints.py.
-- "Archetype" is a cluster of behaviour, not a known model or framework: the product says "behaves like".
CREATE TABLE IF NOT EXISTS wallet_fingerprints (
    wallet              VARCHAR(42) PRIMARY KEY,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    window_start        TIMESTAMPTZ NOT NULL,
    window_end          TIMESTAMPTZ NOT NULL,
    n_swaps             INTEGER NOT NULL,
    is_automated        BOOLEAN NOT NULL,
    is_registered_agent BOOLEAN NOT NULL,
    signals             TEXT[] NOT NULL DEFAULT '{}',
    -- cadence
    median_gap          DOUBLE PRECISION,        -- seconds between consecutive swaps
    gap_cv              DOUBLE PRECISION,
    min_gap             DOUBLE PRECISION,
    period_s            DOUBLE PRECISION,        -- metronomic period: median gap when gap_cv < 0.5, else null
    fast_share          DOUBLE PRECISION,        -- share of swaps within 2 s of the previous one
    -- activity
    hour_hist           INTEGER[] NOT NULL,      -- 24 bins, hour of day UTC
    weekday_hist        INTEGER[] NOT NULL,      -- 7 bins, Monday first
    hour_entropy        DOUBLE PRECISION,        -- nats; ln(24) = 3.18 is uniform
    busiest_block_start INTEGER,                 -- start hour (UTC) of the busiest 4-hour block
    busiest_block_share DOUBLE PRECISION,
    weekend_share       DOUBLE PRECISION,
    us_hours_share      DOUBLE PRECISION,        -- 13:30 to 20:00 UTC
    -- venue
    n_pools             INTEGER NOT NULL,
    top_pool_share      DOUBLE PRECISION,
    stock_share         DOUBLE PRECISION,        -- pools with a stock token
    stable_share        DOUBLE PRECISION,        -- stable/stable pairs
    other_share         DOUBLE PRECISION,
    known_pool_share    DOUBLE PRECISION,        -- share of swaps in pools whose tokens are known
    n_tokens            INTEGER,
    -- sizing
    round_amount_frac   DOUBLE PRECISION,
    repeat_amount_frac  DOUBLE PRECISION,
    log_size_median     DOUBLE PRECISION,        -- log10 of trade size in token1 units, stable token1 only
    log_size_iqr        DOUBLE PRECISION,
    -- behaviour
    reactive_share      DOUBLE PRECISION,
    round_trips_per_100 DOUBLE PRECISION,
    net_flow_frac       DOUBLE PRECISION,
    multi_pool_per_100  DOUBLE PRECISION,
    -- archetype, family, neighbours
    archetype_id        INTEGER,
    archetype_label     TEXT,
    family_id           INTEGER,
    family_size         INTEGER,
    nearest_agents      JSONB,                   -- [{wallet, agent_id, name, distance}] closest registered agents
    features            JSONB                    -- the standardised vector the archetype was fitted on
);
CREATE INDEX IF NOT EXISTS wallet_fingerprints_lower_idx ON wallet_fingerprints (lower(wallet));
CREATE INDEX IF NOT EXISTS wallet_fingerprints_archetype_idx ON wallet_fingerprints (archetype_id);

CREATE TABLE IF NOT EXISTS fingerprint_archetypes (
    archetype_id           INTEGER PRIMARY KEY,
    computed_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    n_wallets              INTEGER NOT NULL,
    label                  TEXT NOT NULL,
    description            TEXT NOT NULL,
    centroid               JSONB NOT NULL,       -- median raw feature values of the members
    example_wallets        TEXT[] NOT NULL DEFAULT '{}',
    registered_agent_names TEXT[] NOT NULL DEFAULT '{}'
);
