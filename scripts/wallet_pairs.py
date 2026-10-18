"""Pairwise wallet correlation over the live window: automated wallets (volume bots excluded) that keep
appearing in the same pool-minute-direction cells, far more often than chance. Writes wallet_pairs and
wallet_clusters (connected components of strongly linked pairs) for the window's end time.

Usage: python scripts/wallet_pairs.py [--hours 48] [--min-together 5] [--min-jaccard 0.2] [--max-cell 150]
Cells with more than --max-cell wallets (mass herds) are skipped: they say "everyone", not "these two".
"""
import os, sys, time
from datetime import datetime, timezone, timedelta
import psycopg
import numpy as np, pandas as pd

def arg(name, default, cast=float): return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default
HOURS = arg("--hours", 48); MIN_TOGETHER = arg("--min-together", 5, int); MIN_J = arg("--min-jaccard", 0.2); MAX_CELL = arg("--max-cell", 150, int)
DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)

def main():
    t0 = time.time()
    with psycopg.connect(DB) as conn:
        conn.execute("SET work_mem = '256MB'")
        end = conn.execute("SELECT max(block_time) FROM swaps").fetchone()[0]
        start = end - timedelta(hours=HOURS)
        import warnings; warnings.filterwarnings("ignore", category=UserWarning)
        df = pd.read_sql("""
            SELECT DISTINCT s.pool, date_trunc('minute', s.block_time) AS minute, s.direction, s.tx_from AS wallet
            FROM swaps s LEFT JOIN wallet_features f ON f.wallet = s.tx_from AND f.is_volume_bot
            WHERE s.block_time > %(a)s AND s.block_time <= %(b)s AND s.is_automated AND f.wallet IS NULL AND NOT s.self_cancel""", conn, params={"a": start, "b": end})
    log(f"{len(df):,} wallet-cells from {df.wallet.nunique():,} automated wallets in {time.time()-t0:.0f}s")
    df["cell"] = df.groupby(["pool", "minute", "direction"]).ngroup()
    size = df.groupby("cell").wallet.transform("size")
    df = df[(size >= 2) & (size <= MAX_CELL)]
    cells_per_wallet = df.groupby("wallet").size()
    n_cells = df.cell.nunique()
    log(f"{n_cells:,} shared cells (2..{MAX_CELL} wallets) after dropping mass herds")
    # pairs inside each cell: self-merge on cell, keep a < b
    m = df[["cell", "wallet"]].merge(df[["cell", "wallet"]], on="cell", suffixes=("_a", "_b"))
    m = m[m.wallet_a < m.wallet_b]
    pairs = m.groupby(["wallet_a", "wallet_b"]).size().rename("together").reset_index()
    pairs = pairs[pairs.together >= MIN_TOGETHER]
    pairs["cells_a"] = pairs.wallet_a.map(cells_per_wallet); pairs["cells_b"] = pairs.wallet_b.map(cells_per_wallet)
    pairs["jaccard"] = pairs.together / (pairs.cells_a + pairs.cells_b - pairs.together)
    pairs["lift"] = pairs.together * n_cells / (pairs.cells_a * pairs.cells_b)
    strong = pairs[pairs.jaccard >= MIN_J].copy()
    log(f"{len(pairs):,} pairs together >= {MIN_TOGETHER}; {len(strong):,} with jaccard >= {MIN_J}")
    # clusters: union-find over strong pairs
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    for a, b in zip(strong.wallet_a, strong.wallet_b):
        ra, rb = find(a), find(b)
        if ra != rb: parent[ra] = rb
    members = {}
    for w in set(strong.wallet_a) | set(strong.wallet_b): members.setdefault(find(w), []).append(w)
    clusters = sorted(members.values(), key=len, reverse=True)
    log(f"{len(clusters):,} clusters; largest {[len(c) for c in clusters[:8]]}")
    with psycopg.connect(DB) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM wallet_pairs WHERE window_end = %s", (end,)); cur.execute("DELETE FROM wallet_clusters WHERE window_end = %s", (end,))
        with cur.copy("COPY wallet_pairs (window_end, wallet_a, wallet_b, together, cells_a, cells_b, jaccard, lift) FROM STDIN") as cp:
            for r in strong.itertuples(index=False):
                cp.write_row((end, r.wallet_a, r.wallet_b, int(r.together), int(r.cells_a), int(r.cells_b), float(r.jaccard), float(r.lift)))
        with cur.copy("COPY wallet_clusters (window_end, cluster_id, wallet, size) FROM STDIN") as cp:
            for i, c in enumerate(clusters):
                for w in c: cp.write_row((end, i, w, len(c)))
        conn.commit()
    in_clusters = sum(len(c) for c in clusters)
    log(f"stored: {len(strong):,} pairs, {in_clusters:,} wallets in {len(clusters):,} clusters (window {start:%d %b %H:%M} to {end:%d %b %H:%M} UTC) in {time.time()-t0:.0f}s")
    top = strong.sort_values("together", ascending=False).head(8)
    for r in top.itertuples(index=False):
        log(f"  {r.wallet_a[:10]}… + {r.wallet_b[:10]}…: together {r.together} of {r.cells_a}/{r.cells_b} cells, jaccard {r.jaccard:.2f}, lift {r.lift:,.0f}")

if __name__ == "__main__":
    main()
