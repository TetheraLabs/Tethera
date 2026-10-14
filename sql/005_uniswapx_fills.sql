-- Fills on the chain's UniswapX V3DutchOrderReactor: who fills, for whom, when. The market the Tethera filler enters.
CREATE TABLE IF NOT EXISTS uniswapx_fills (
    tx_hash      VARCHAR(66) NOT NULL,
    log_index    INTEGER     NOT NULL,
    block_number BIGINT      NOT NULL,
    block_time   TIMESTAMPTZ,
    order_hash   VARCHAR(66) NOT NULL,
    filler       VARCHAR(42) NOT NULL,
    swapper      VARCHAR(42) NOT NULL,
    nonce        NUMERIC(78,0),
    PRIMARY KEY (tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS uniswapx_fills_filler_idx ON uniswapx_fills (filler);
CREATE INDEX IF NOT EXISTS uniswapx_fills_time_idx   ON uniswapx_fills (block_time);
