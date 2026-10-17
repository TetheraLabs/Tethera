-- API keys for the public crowding endpoint. Only a hash of each key is stored.
-- Issue one: INSERT INTO api_keys (key_hash, label, requests_per_minute) VALUES (encode(sha256('the-key'::bytea), 'hex'), 'who', 600);
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash            CHAR(64) PRIMARY KEY,       -- sha256 hex of the key
    label               TEXT NOT NULL,
    requests_per_minute INTEGER NOT NULL DEFAULT 300,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    disabled            BOOLEAN NOT NULL DEFAULT FALSE
);
