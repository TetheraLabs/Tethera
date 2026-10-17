-- Pairwise wallet correlation: which automated wallets keep turning up in the same pool, minute and
-- direction. The seed of the coordination detector: a herd of strangers looks different from a family.
CREATE TABLE IF NOT EXISTS wallet_pairs (
    window_end   TIMESTAMPTZ NOT NULL,
    wallet_a     VARCHAR(42) NOT NULL,
    wallet_b     VARCHAR(42) NOT NULL,      -- wallet_a < wallet_b
    together     INTEGER NOT NULL,          -- cells (pool, minute, direction) both were in
    cells_a      INTEGER NOT NULL,
    cells_b      INTEGER NOT NULL,
    jaccard      DOUBLE PRECISION NOT NULL, -- together / (cells_a + cells_b - together)
    lift         DOUBLE PRECISION NOT NULL, -- together / expected under independence
    PRIMARY KEY (window_end, wallet_a, wallet_b)
);
CREATE TABLE IF NOT EXISTS wallet_clusters (
    window_end   TIMESTAMPTZ NOT NULL,
    cluster_id   INTEGER NOT NULL,
    wallet       VARCHAR(42) NOT NULL,
    size         INTEGER NOT NULL,
    PRIMARY KEY (window_end, wallet)
);
CREATE INDEX IF NOT EXISTS wallet_clusters_cluster_idx ON wallet_clusters (window_end, cluster_id);
