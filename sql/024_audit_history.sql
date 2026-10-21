-- Every computed audit is kept, so a token's verdict and shares can be read back over time (one row per computation).
CREATE TABLE IF NOT EXISTS token_audit_history (
    token       varchar(42) NOT NULL,
    days        real NOT NULL,
    computed_at timestamptz NOT NULL DEFAULT now(),
    verdict     text,
    swaps       bigint, wallets bigint,
    automated_swap_share real, printed_share real, fresh_wallet_share real, top10_share real, organic_volume_usd numeric,
    PRIMARY KEY (token, days, computed_at)
);
