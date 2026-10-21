-- Access by burning the project token, per-key fingerprints, revocation reasons, and the desk's decision record.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS wallet varchar(42);
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS tier text;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS fingerprint integer;           -- per-key seed for output fingerprinting
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS revoked_reason text;
CREATE TABLE IF NOT EXISTS key_burns (
    tx_hash    varchar(66) NOT NULL,
    log_index  integer NOT NULL,
    key_hash   char(64) NOT NULL REFERENCES api_keys (key_hash),
    wallet     varchar(42) NOT NULL,
    amount     numeric(40, 18) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tx_hash, log_index)
);
-- What the desk did: one row per decision, written by the paper engine, served with a delay through a keyed feed.
CREATE TABLE IF NOT EXISTS desk_record (
    id         bigserial PRIMARY KEY,
    at         timestamptz NOT NULL,
    strategy   text NOT NULL,
    symbol     text NOT NULL,
    action     text NOT NULL,            -- enter_long, enter_short, exit, resize
    size       double precision NOT NULL, -- target position after the action, -1 to 1
    price      double precision,          -- fill price on chain
    reason     jsonb,
    record_hash char(64) NOT NULL         -- sha256 of the row content, chained to the previous row's hash
);
CREATE INDEX IF NOT EXISTS desk_record_at_idx ON desk_record (at);
