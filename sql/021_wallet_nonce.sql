-- Transaction count of a wallet as seen on the chain (eth_getTransactionCount), fetched when an audit needs it.
-- A wallet with a count of one or two was funded, traded once, and did nothing else, whatever our own tables start at.
CREATE TABLE IF NOT EXISTS wallet_nonce (
    wallet     VARCHAR(42) PRIMARY KEY,
    nonce      BIGINT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
