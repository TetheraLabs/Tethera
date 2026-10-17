"""Week-2 test: do automated wallets that trade inside a crowd do worse than the same kind of
wallet trading alone?

Unit of analysis: one (pool, minute, direction) cell with at least one automated, non-volume-bot
wallet, from pool_minutes_all (live + history). Crowding = automated wallets on that side in that
minute; one-sidedness = same / (same + opposite). Prices are token1 per token0, from the pool's own
minute series (px_last of the latest row in each minute). Everything is signed so that a positive
number is bad for the trader at entry and good for them afterwards:

  entry_impact = sign * (px_median(cell) / px_prev - 1)      px_prev = last price before the minute
  ret_1h       = sign * (px(t+60m) / px_median(cell) - 1)    sign = +1 for buy0, -1 for sell0
  ret_24h      = sign * (px(t+24h) / px_median(cell) - 1)

Buckets: 1, 2-4, 5-14, 15+ automated wallets, split by one-sided (>= 0.85) or not, and by pool kind
(stock / stable-eth hub / other) from the tokens table. Output: reports/outcome_study.md and a CSV
of every cell under data/. Reads pool_minutes_all only (bounded by time), never swaps_archive.

Usage: python scripts/outcome_study.py --from 2026-07-01 --to 2026-08-17 [--min-cells 200] [--out reports/outcome_study.md]
"""
import os, sys, time, math
from datetime import datetime, timezone, timedelta
import psycopg
import numpy as np, pandas as pd

def arg(name, default): return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default
FROM = datetime.fromisoformat(arg("--from", "2026-07-01")).replace(tzinfo=timezone.utc)
TO = datetime.fromisoformat(arg("--to", "2026-08-17")).replace(tzinfo=timezone.utc)
MIN_CELLS = int(arg("--min-cells", "200"))
OUT = arg("--out", os.path.join(os.path.dirname(__file__), "..", "reports", "outcome_study.md"))
DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)
BUCKETS = [(1, 1, "1"), (2, 4, "2-4"), (5, 14, "5-14"), (15, 10**9, "15+")]

def bucket(n):
    for lo, hi, name in BUCKETS:
        if lo <= n <= hi: return name

def pool_kind(conn):
    rows = conn.execute("""SELECT p.pool, a.kind, b.kind FROM pools p
                           LEFT JOIN tokens a ON a.token = p.token0 LEFT JOIN tokens b ON b.token = p.token1""").fetchall()
    out = {}
    for pool, k0, k1 in rows:
        ks = {k0, k1}
        if "stock" in ks: out[pool] = "stock"
        elif ks <= {"stable", "eth"} and ks: out[pool] = "eth-stable hub"
        elif k0 is None and k1 is None: out[pool] = "unknown"
        else: out[pool] = "other"
    return out

def main():
    t0 = time.time()
    with psycopg.connect(DB) as conn:
        conn.execute("SET work_mem = '256MB'")
        kinds = pool_kind(conn)
        log(f"{len(kinds):,} pools with a kind; loading minutes {FROM:%Y-%m-%d} to {TO:%Y-%m-%d}")
        df = pd.read_sql("""
            SELECT pool, minute, direction, swaps, wallets, auto_swaps, auto_wallets,
                   px_median::float8 AS px_median, px_last::float8 AS px_last, last_block
            FROM pool_minutes_all WHERE minute >= %(a)s AND minute < %(b)s + interval '24 hours' AND px_median IS NOT NULL
        """, conn, params={"a": FROM, "b": TO})
    log(f"{len(df):,} pool-minute rows in {time.time()-t0:.0f}s")
    df["minute"] = pd.to_datetime(df["minute"], utc=True)
    df = df[np.isfinite(df.px_median) & (df.px_median > 0)]
    # one price per pool-minute: the last swap of the minute, whichever direction
    px = (df.sort_values(["pool", "minute", "last_block"]).groupby(["pool", "minute"], as_index=False).last()[["pool", "minute", "px_last"]])
    cells = df[(df.auto_wallets >= 1) & (df.minute < TO)].copy()
    opp = df[["pool", "minute", "direction", "auto_wallets"]].copy()
    opp["direction"] = np.where(opp.direction == "buy0", "sell0", "buy0")
    cells = cells.merge(opp.rename(columns={"auto_wallets": "auto_opp"}), on=["pool", "minute", "direction"], how="left").fillna({"auto_opp": 0})
    cells["one_sided"] = cells.auto_wallets / (cells.auto_wallets + cells.auto_opp)
    cells["sign"] = np.where(cells.direction == "buy0", 1.0, -1.0)
    cells["bucket"] = cells.auto_wallets.map(bucket)
    cells["kind"] = cells.pool.map(kinds).fillna("unknown")
    # price before, 1h after, 24h after, via per-pool as-of merges
    out = []
    for pool, g in cells.groupby("pool", sort=False):
        series = px[px.pool == pool].sort_values("minute")
        if len(series) < 3: continue
        s_min = series.minute.values; s_px = series.px_last.values
        g = g.sort_values("minute")
        m = g.minute.values
        i_prev = np.searchsorted(s_min, m, side="left") - 1            # strictly before the minute
        i_1h = np.searchsorted(s_min, m + np.timedelta64(60, "m"), side="right") - 1
        i_24h = np.searchsorted(s_min, m + np.timedelta64(24, "h"), side="right") - 1
        ok = i_prev >= 0
        g = g[ok].copy(); i_prev, i_1h, i_24h = i_prev[ok], i_1h[ok], i_24h[ok]
        g["px_prev"] = s_px[i_prev]
        # require that the 1h/24h price actually comes from after the cell (not the same stale price)
        g["px_1h"] = np.where(s_min[i_1h] > g.minute.values, s_px[i_1h], np.nan)
        g["px_24h"] = np.where(s_min[i_24h] > g.minute.values + np.timedelta64(30, "m"), s_px[i_24h], np.nan)
        out.append(g)
    cells = pd.concat(out, ignore_index=True)
    cells["entry_impact"] = cells.sign * (cells.px_median / cells.px_prev - 1)
    cells["ret_1h"] = cells.sign * (cells.px_1h / cells.px_median - 1)
    cells["ret_24h"] = cells.sign * (cells.px_24h / cells.px_median - 1)
    # winsorise the tails (bad decimals, dust swaps) so a few absurd ratios can't drive the means
    for c in ("entry_impact", "ret_1h", "ret_24h"):
        lo, hi = cells[c].quantile([0.005, 0.995]); cells[c] = cells[c].clip(lo, hi)
    cells["sided"] = np.where(cells.one_sided >= 0.85, "one-sided", "two-sided")
    cells.to_csv(os.path.join(os.path.dirname(__file__), "..", "data", f"outcome_cells_{FROM:%Y%m%d}_{TO:%Y%m%d}.csv.gz"), index=False, compression="gzip")
    log(f"{len(cells):,} cells scored")

    def table(g, by):
        agg = g.groupby(by, observed=True).agg(cells=("pool", "size"), pools=("pool", "nunique"), auto_swaps=("auto_swaps", "sum"),
                                                impact_bp=("entry_impact", lambda x: 1e4 * x.median()), impact_mean_bp=("entry_impact", lambda x: 1e4 * x.mean()),
                                                ret1h_bp=("ret_1h", lambda x: 1e4 * x.median()), ret1h_mean_bp=("ret_1h", lambda x: 1e4 * x.mean()),
                                                ret24h_bp=("ret_24h", lambda x: 1e4 * x.median()), ret24h_mean_bp=("ret_24h", lambda x: 1e4 * x.mean()),
                                                win1h=("ret_1h", lambda x: (x > 0).mean()))
        agg = agg[agg.cells >= MIN_CELLS]
        return agg

    order = [b[2] for b in BUCKETS]
    cells["bucket"] = pd.Categorical(cells.bucket, order, ordered=True)
    lines = [f"# Outcome study: crowded vs uncrowded automated trades\n",
             f"Window {FROM:%d %b %Y} to {TO:%d %b %Y} UTC. {len(cells):,} pool-minute-direction cells with at least one automated wallet "
             f"(volume bots excluded), {cells.pool.nunique():,} pools. Numbers in basis points; medians unless marked mean. "
             f"`impact` is the price paid at entry relative to the last price before the minute, signed so positive = paid more. "
             f"`ret1h`/`ret24h` are the mark-to-market return of the trade one hour and one day later, positive = profit. "
             f"Tails winsorised at 0.5%/99.5%. Cells with fewer than {MIN_CELLS} rows are hidden.\n"]
    def md(df_):
        cols = list(df_.columns); hdr = "| " + " | ".join([" / ".join(map(str, df_.index.names))] + cols) + " |"
        sep = "|" + "---|" * (len(cols) + 1)
        rows = []
        for idx, r in df_.iterrows():
            idx = idx if isinstance(idx, tuple) else (idx,)
            vals = [f"{v:,.0f}" if isinstance(v, (int, np.integer)) or (isinstance(v, float) and abs(v) >= 100) else (f"{v:.2f}" if isinstance(v, float) else str(v)) for v in r.values]
            rows.append("| " + " | ".join([" / ".join(map(str, idx))] + vals) + " |")
        return "\n".join([hdr, sep] + rows)
    lines.append("## All pools, by crowd size\n"); lines.append(md(table(cells, ["bucket"])))
    lines.append("\n## By crowd size and one-sidedness (one-sided = at least 85% of automated wallets on the same side)\n"); lines.append(md(table(cells, ["sided", "bucket"])))
    lines.append("\n## By pool kind and crowd size\n"); lines.append(md(table(cells, ["kind", "bucket"])))
    lines.append("\n## Stock-token pools, one-sided only\n"); lines.append(md(table(cells[(cells.kind == "stock") & (cells.sided == "one-sided")], ["bucket"])))
    # within-pool comparison: for every pool that has both solo and crowded cells, the difference of medians,
    # then the median of those differences across pools (removes the 'big pools have small impact' confound)
    def within_pool(sub, crowd=("5-14", "15+")):
        rows = []
        for pool, g in sub.groupby("pool", sort=False):
            a = g[g.bucket == "1"]; b = g[g.bucket.isin(crowd)]
            if len(a) >= 20 and len(b) >= 10:
                rows.append({"pool": pool, "kind": g.kind.iloc[0], "solo": len(a), "crowd": len(b),
                             "d_impact": 1e4 * (b.entry_impact.median() - a.entry_impact.median()),
                             "d_ret1h": 1e4 * (b.ret_1h.median() - a.ret_1h.median()),
                             "d_ret24h": 1e4 * (b.ret_24h.median() - a.ret_24h.median())})
        if not rows: return None
        w = pd.DataFrame(rows)
        out = w.groupby("kind").agg(pools=("pool", "size"), d_impact_bp=("d_impact", "median"), d_ret1h_bp=("d_ret1h", "median"), d_ret24h_bp=("d_ret24h", "median"),
                                    share_worse_entry=("d_impact", lambda x: (x > 0).mean()), share_worse_1h=("d_ret1h", lambda x: (x < 0).mean()))
        allrow = pd.DataFrame([{"pools": len(w), "d_impact_bp": w.d_impact.median(), "d_ret1h_bp": w.d_ret1h.median(), "d_ret24h_bp": w.d_ret24h.median(),
                                "share_worse_entry": (w.d_impact > 0).mean(), "share_worse_1h": (w.d_ret1h < 0).mean()}], index=pd.Index(["all"], name="kind"))
        return pd.concat([allrow, out])
    lines.append("\n## Within-pool: crowded (5+) minus solo, same pool, median across pools (basis points)\n")
    lines.append("Positive d_impact = the crowd paid more at entry than solo trades in the same pool; negative d_ret = the crowd's trades were worth less afterwards. share_worse = share of pools where the crowd did worse.\n")
    for label, sub in (("all cells", cells), ("one-sided cells only", cells[cells.sided == "one-sided"])):
        w = within_pool(sub)
        lines.append(f"\n**{label}**\n"); lines.append(md(w) if w is not None else "not enough pools")
    # the headline gap
    def gap(sub, col):
        a = sub[sub.bucket == "1"][col]; b = sub[sub.bucket == "15+"][col]
        if len(a) < MIN_CELLS or len(b) < MIN_CELLS: return None
        # bootstrap CI for the difference of means
        rng = np.random.default_rng(0); diffs = [rng.choice(b.dropna(), len(b.dropna())).mean() - rng.choice(a.dropna(), len(a.dropna())).mean() for _ in range(300)]
        return b.mean() - a.mean(), np.percentile(diffs, 2.5), np.percentile(diffs, 97.5), len(a), len(b)
    lines.append("\n## Headline: 15+ crowd minus solo (mean, with 95% bootstrap interval)\n")
    for label, sub in (("all one-sided", cells[cells.sided == "one-sided"]), ("stock pools one-sided", cells[(cells.kind == "stock") & (cells.sided == "one-sided")]),
                       ("other pools one-sided", cells[(cells.kind == "other") & (cells.sided == "one-sided")]), ("eth-stable hub", cells[cells.kind == "eth-stable hub"])):
        for col in ("entry_impact", "ret_1h", "ret_24h"):
            r = gap(sub, col)
            if r: lines.append(f"- {label}, {col}: {1e4*r[0]:+.1f} bp [{1e4*r[1]:+.1f}, {1e4*r[2]:+.1f}] (solo n={r[3]:,}, crowd n={r[4]:,})")
            else: lines.append(f"- {label}, {col}: not enough cells")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT, "w").write("\n".join(lines) + "\n")
    log(f"wrote {OUT} in {time.time()-t0:.0f}s")

if __name__ == "__main__":
    main()
