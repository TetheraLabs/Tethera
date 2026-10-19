-- Keys can expire (paid tiers run 30 days); payments are recorded by transaction hash so one transfer buys one key.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS expires_at timestamptz;
CREATE TABLE IF NOT EXISTS key_payments (
    tx_hash    varchar(66) PRIMARY KEY,
    key_hash   char(64) NOT NULL REFERENCES api_keys (key_hash),
    payer      varchar(42),
    usdg       numeric(18, 6) NOT NULL,
    tier       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
