CREATE TABLE IF NOT EXISTS tokens (
    token       VARCHAR(42) PRIMARY KEY,
    symbol      TEXT,
    name        TEXT,
    decimals    INTEGER,
    kind        TEXT,              -- 'stock' | 'stable' | 'eth' | 'other' | null until classified
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
