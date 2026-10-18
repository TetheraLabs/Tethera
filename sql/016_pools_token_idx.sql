-- Token lookups on pools (classification, fingerprints, fee simulation) were sequential scans over a million rows.
CREATE INDEX IF NOT EXISTS pools_token0_idx ON pools (token0);
CREATE INDEX IF NOT EXISTS pools_token1_idx ON pools (token1);
