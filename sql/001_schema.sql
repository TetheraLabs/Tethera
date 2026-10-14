-- Tethera core tables. Robinhood Chain mainnet (4663).

CREATE TABLE IF NOT EXISTS agents (
    agent_id        BIGINT PRIMARY KEY,
    owner           VARCHAR(42) NOT NULL,       -- owner at registration
    current_owner   VARCHAR(42),                -- ownerOf() now; NFT may have moved
    agent_wallet    VARCHAR(42),                -- getAgentWallet(); null if unset
    agent_uri       TEXT NOT NULL,
    profile         JSONB,                      -- decoded registration JSON, if inline
    block_number    BIGINT NOT NULL,
    tx_hash         VARCHAR(66) NOT NULL,
    log_index       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS agents_wallet_idx ON agents (agent_wallet);
CREATE INDEX IF NOT EXISTS agents_owner_idx  ON agents (owner);

CREATE TABLE IF NOT EXISTS pools (
    pool            VARCHAR(66) PRIMARY KEY,    -- address (v2/v3) or 32-byte pool id (v4)
    protocol        VARCHAR(2),                 -- 'v2' | 'v3' | 'v4'
    token0          VARCHAR(42) NOT NULL,
    token1          VARCHAR(42) NOT NULL,
    fee             INTEGER,
    tick_spacing    INTEGER,
    hooks           VARCHAR(42),
    created_block   BIGINT
);

CREATE TABLE IF NOT EXISTS swaps (
    protocol        VARCHAR(2)  NOT NULL,
    pool            VARCHAR(66) NOT NULL,
    block_number    BIGINT      NOT NULL,
    block_time      TIMESTAMPTZ NOT NULL,
    tx_hash         VARCHAR(66) NOT NULL,
    log_index       INTEGER     NOT NULL,
    event_sender    VARCHAR(42) NOT NULL,       -- usually a router
    recipient       VARCHAR(42),                -- null for v4
    tx_from         VARCHAR(42) NOT NULL,       -- the wallet that sent the transaction
    amount0         NUMERIC(78,0) NOT NULL,     -- +ve = token0 into pool (user sold token0)
    amount1         NUMERIC(78,0) NOT NULL,
    direction       VARCHAR(5)  NOT NULL,       -- 'sell0' | 'buy0'
    is_agent        BOOLEAN,                    -- set by the labelling step
    agent_id        BIGINT REFERENCES agents (agent_id),
    PRIMARY KEY (tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS swaps_pool_time_idx ON swaps (pool, block_time);
CREATE INDEX IF NOT EXISTS swaps_from_idx      ON swaps (tx_from);
CREATE INDEX IF NOT EXISTS swaps_agent_idx     ON swaps (is_agent) WHERE is_agent;
