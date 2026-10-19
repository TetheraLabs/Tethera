-- Pons launchpad: launches and bonding-curve trades. Before a token graduates to Uniswap it trades on its own curve
-- contract, invisible to anything that only reads Uniswap swaps (including our follower until now).
CREATE TABLE IF NOT EXISTS launches (
    token        VARCHAR(42) PRIMARY KEY,
    curve        VARCHAR(42) NOT NULL,
    deployer     VARCHAR(42) NOT NULL,
    pair_token   VARCHAR(42) NOT NULL,
    threshold    NUMERIC(78,0),
    block_number BIGINT NOT NULL,
    block_time   TIMESTAMPTZ NOT NULL,
    tx_hash      VARCHAR(66) NOT NULL,
    graduated_block BIGINT,
    graduated_time  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS launches_curve_idx ON launches (curve);
CREATE INDEX IF NOT EXISTS launches_time_idx ON launches (block_time);
CREATE TABLE IF NOT EXISTS curve_trades (
    tx_hash      VARCHAR(66) NOT NULL,
    log_index    INTEGER NOT NULL,
    curve        VARCHAR(42) NOT NULL,
    token        VARCHAR(42),
    side         TEXT NOT NULL,               -- 'buy' | 'sell'
    trader       VARCHAR(42) NOT NULL,        -- buyer/seller (indexed in the event)
    recipient    VARCHAR(42) NOT NULL,
    quote_amount NUMERIC(78,0) NOT NULL,      -- quoteIn for buys, quoteOut for sells
    token_amount NUMERIC(78,0) NOT NULL,
    fee          NUMERIC(78,0) NOT NULL,
    tax          NUMERIC(78,0) NOT NULL,
    block_number BIGINT NOT NULL,
    block_time   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS curve_trades_token_time_idx ON curve_trades (token, block_time);
CREATE INDEX IF NOT EXISTS curve_trades_trader_idx ON curve_trades (trader);
CREATE INDEX IF NOT EXISTS curve_trades_time_idx ON curve_trades (block_time);
