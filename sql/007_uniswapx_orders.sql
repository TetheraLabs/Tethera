-- Open UniswapX orders seen on the order feed, with their decay curves, so netting can be replayed later.
CREATE TABLE IF NOT EXISTS uniswapx_orders (
    order_hash     VARCHAR(66) PRIMARY KEY,
    first_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at     TIMESTAMPTZ,
    swapper        VARCHAR(42),
    input_token    VARCHAR(42),
    input_start    NUMERIC(78,0),
    input_max      NUMERIC(78,0),
    output_token   VARCHAR(42),
    output_start   NUMERIC(78,0),
    output_min     NUMERIC(78,0),
    output_recipient VARCHAR(42),
    decay_blocks   INTEGER,
    cosigned       BOOLEAN,
    last_status    TEXT,
    fill_tx        VARCHAR(66),
    fill_block     BIGINT,
    raw            JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS uniswapx_orders_created_idx ON uniswapx_orders (created_at);
