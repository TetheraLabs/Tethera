"""Wallet fingerprints: a behavioural profile for every automated or registered wallet over the last week,
clustered into archetypes. An archetype is a cluster of behaviour (cadence, hours, venues, sizing), not a
known model or framework: the registry carries no such ground truth, so the product says "behaves like".

Usage: python scripts/fingerprints.py [--hours 168] [--k auto|N] [--min-swaps 10] [--stability] [--report]
  --stability  cluster each half of the window separately and report the adjusted Rand index between them
  --report     write reports/fingerprints.md
Writes wallet_fingerprints (upsert) and fingerprint_archetypes (replaced). Reads swaps only for the wallet
set and the window, through the tx_from index; never touches swaps_archive.
"""
import math, os, sys, time, warnings
from datetime import timedelta, timezone
import psycopg
from psycopg.types.json import Jsonb
import numpy as np, pandas as pd
warnings.filterwarnings("ignore", category=UserWarning)      # pandas on a plain DBAPI connection

def arg(name, default, cast=float): return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default
HOURS = arg("--hours", 168); K = arg("--k", "auto", str); MIN_SWAPS = arg("--min-swaps", 10, int)
STABILITY = "--stability" in sys.argv; REPORT = "--report" in sys.argv
DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
REPORT_PATH = os.path.join(os.path.dirname(__file__), "..", "reports", "fingerprints.md")
SEED = 42
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)

HOUR_COLS = ", ".join(f"count(*) FILTER (WHERE hr = {h}) AS h{h}" for h in range(24))
DOW_COLS = ", ".join(f"count(*) FILTER (WHERE dow = {d}) AS d{d}" for d in range(7))

# One row per (wallet, bucket). {bucket} is a constant for the full window or a half split for --stability.
PROFILE_SQL = """
WITH s AS (
    SELECT s.tx_from AS w, {bucket} AS bucket, s.pool, s.block_number, s.log_index, s.direction,
           extract(epoch FROM s.block_time) AS t,
           extract(hour FROM s.block_time AT TIME ZONE 'UTC')::int AS hr,
           extract(isodow FROM s.block_time AT TIME ZONE 'UTC')::int - 1 AS dow,
           CASE WHEN s.amount0 > 0 THEN s.amount0 ELSE s.amount1 END AS amount_in, s.amount0,
           CASE WHEN t0.kind = 'stock' OR t1.kind = 'stock' THEN 'stock'
                WHEN t0.kind = 'stable' AND t1.kind = 'stable' THEN 'stable'
                WHEN p.pool IS NULL THEN NULL ELSE 'other' END AS venue,
           CASE WHEN t1.kind = 'stable' AND s.amount1 <> 0 AND t1.decimals IS NOT NULL
                THEN log(abs(s.amount1) / power(10::numeric, t1.decimals)) END AS log_size,
           p.token0, p.token1
    FROM swaps s LEFT JOIN pools p USING (pool)
    LEFT JOIN tokens t0 ON t0.token = p.token0 LEFT JOIN tokens t1 ON t1.token = p.token1
    WHERE s.tx_from = ANY(%(wallets)s) AND s.block_time >= %(a)s AND s.block_time < %(b)s AND NOT s.self_cancel
),
ordered AS (SELECT w, bucket, t - lag(t) OVER (PARTITION BY w, bucket ORDER BY block_number, log_index) AS gap FROM s),
gaps AS (
    SELECT w, bucket, percentile_cont(0.5) WITHIN GROUP (ORDER BY gap) AS median_gap, min(gap) AS min_gap,
           stddev_samp(gap) / nullif(avg(gap), 0) AS gap_cv, avg((gap <= 2)::int) AS fast_share
    FROM ordered WHERE gap IS NOT NULL GROUP BY 1, 2),
per AS (
    SELECT w, bucket, count(*) AS n_swaps, count(DISTINCT pool) AS n_pools,
           {hours}, {dows},
           avg((dow >= 5)::int) AS weekend_share,
           avg((venue = 'stock')::int) AS stock_share, avg((venue = 'stable')::int) AS stable_share,
           avg((venue = 'other')::int) AS other_share, avg((venue IS NOT NULL)::int) AS known_pool_share,
           avg(CASE WHEN amount_in > 0 AND mod(amount_in, power(10::numeric, greatest(length(amount_in::text) - 3, 0))) = 0 THEN 1.0 ELSE 0.0 END) AS round_amount_frac,
           1.0 - count(DISTINCT amount_in)::float / count(*) AS repeat_amount_frac,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY log_size) AS log_size_median,
           percentile_cont(0.75) WITHIN GROUP (ORDER BY log_size) - percentile_cont(0.25) WITHIN GROUP (ORDER BY log_size) AS log_size_iqr
    FROM s GROUP BY 1, 2),
toks AS (SELECT w, bucket, count(DISTINCT tok) AS n_tokens FROM (SELECT w, bucket, unnest(ARRAY[token0, token1]) AS tok FROM s) x WHERE tok IS NOT NULL GROUP BY 1, 2),
blocks AS (
    SELECT w, bucket, count(*) FILTER (WHERE npools > 1) AS multi_pool_blocks
    FROM (SELECT w, bucket, block_number, count(DISTINCT pool) AS npools FROM s GROUP BY 1, 2, 3) b GROUP BY 1, 2),
rt AS (
    SELECT w, bucket, count(*) AS round_trips FROM (
        SELECT w, bucket, direction, t,
               lag(direction) OVER (PARTITION BY w, bucket, pool ORDER BY block_number, log_index) AS pd,
               lag(t) OVER (PARTITION BY w, bucket, pool ORDER BY block_number, log_index) AS pt
        FROM s) x
    WHERE pd IS NOT NULL AND pd <> direction AND t - pt <= 30 GROUP BY 1, 2),
flow AS (
    SELECT w, bucket, sum(abs(net0))::float / nullif(sum(gross0), 0) AS net_flow_frac, max(cnt)::float / sum(cnt) AS top_pool_share
    FROM (SELECT w, bucket, pool, sum(amount0) AS net0, sum(abs(amount0)) AS gross0, count(*) AS cnt FROM s GROUP BY 1, 2, 3) x GROUP BY 1, 2)
SELECT p.*, g.median_gap, g.min_gap, g.gap_cv, g.fast_share, coalesce(k.n_tokens, 0) AS n_tokens,
       coalesce(b.multi_pool_blocks, 0) AS multi_pool_blocks, coalesce(r.round_trips, 0) AS round_trips,
       f.net_flow_frac, f.top_pool_share
FROM per p LEFT JOIN gaps g USING (w, bucket) LEFT JOIN toks k USING (w, bucket) LEFT JOIN blocks b USING (w, bucket)
LEFT JOIN rt r USING (w, bucket) LEFT JOIN flow f USING (w, bucket)
WHERE p.n_swaps >= %(min_swaps)s
"""

# the numeric vector the archetypes are fitted on (robust-scaled, clipped); log columns are log10(1 + x)
VECTOR = ["log_n_swaps", "log_median_gap", "gap_cv", "log_min_gap", "fast_share", "hour_entropy", "weekend_share",
          "us_hours_share", "log_n_pools", "top_pool_share", "stock_share", "stable_share", "log_n_tokens",
          "round_amount_frac", "repeat_amount_frac", "log_size_median", "log_size_iqr",
          "reactive_share", "round_trips_per_100", "net_flow_frac", "multi_pool_per_100"]
RAW = ["n_swaps", "median_gap", "gap_cv", "min_gap", "fast_share", "hour_entropy", "busiest_block_start", "busiest_block_share",
       "weekend_share", "us_hours_share", "n_pools", "top_pool_share", "stock_share", "stable_share", "other_share", "n_tokens",
       "round_amount_frac", "repeat_amount_frac", "log_size_median", "log_size_iqr",
       "reactive_share", "round_trips_per_100", "net_flow_frac", "multi_pool_per_100"]


def profiles(conn, wallets, a, b, mid=None):
    """Per-wallet profile rows from swaps in [a, b); split at mid into buckets 0/1 when given."""
    bucket = "CASE WHEN s.block_time < %(mid)s THEN 0 ELSE 1 END" if mid else "0"
    sql = PROFILE_SQL.format(bucket=bucket, hours=HOUR_COLS, dows=DOW_COLS)
    df = pd.read_sql(sql, conn, params={"wallets": list(wallets), "a": a, "b": b, "mid": mid, "min_swaps": MIN_SWAPS})
    return derive(df)


def derive(df):
    """Histograms, entropy, busiest block and the per-100 rates; then the standardised vector columns."""
    H = df[[f"h{h}" for h in range(24)]].to_numpy(float); D = df[[f"d{d}" for d in range(7)]].to_numpy(float)
    df["hour_hist"] = [list(map(int, r)) for r in H]; df["weekday_hist"] = [list(map(int, r)) for r in D]
    p = H / np.maximum(H.sum(1, keepdims=True), 1)
    df["hour_entropy"] = -(np.where(p > 0, p * np.log(np.where(p > 0, p, 1)), 0)).sum(1)
    blocks = np.stack([np.roll(p, -i, axis=1)[:, :4].sum(1) for i in range(24)], 1)   # block i = hours i..i+3
    df["busiest_block_start"] = blocks.argmax(1); df["busiest_block_share"] = blocks.max(1)
    # 13:30-20:00 UTC, counted by whole hours 14..19 plus half of hour 13
    df["us_hours_share"] = (H[:, 14:20].sum(1) + 0.5 * H[:, 13]) / np.maximum(H.sum(1), 1)
    df = df.drop(columns=[f"h{h}" for h in range(24)] + [f"d{d}" for d in range(7)] )
    df["round_trips_per_100"] = 100 * df.round_trips / df.n_swaps
    df["multi_pool_per_100"] = 100 * df.multi_pool_blocks / df.n_swaps
    for c in ["n_swaps", "median_gap", "min_gap", "n_pools", "n_tokens"]:
        df["log_" + c] = np.log10(1 + df[c].astype(float))
    return df


def standardise(df):
    """Robust scaling (median / IQR) of VECTOR, nulls imputed with the column median, clipped to +-5."""
    X = df[VECTOR].astype(float).copy()
    X = X.fillna(X.median()).fillna(0)
    med = X.median(); iqr = (X.quantile(0.75) - X.quantile(0.25)).replace(0, 1.0)
    Z = ((X - med) / iqr).clip(-5, 5)
    return Z.to_numpy()


def cluster(Z, k):
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score
    if k == "auto":
        best = None
        for kk in range(4, 13):
            lab = KMeans(kk, n_init=10, random_state=SEED).fit_predict(Z)
            sc = silhouette_score(Z, lab, sample_size=min(len(Z), 5000), random_state=SEED)
            log(f"  k={kk}: silhouette {sc:.3f}")
            if best is None or sc > best[0]: best = (sc, kk, lab)
        sc, k, lab = best
        log(f"chose k={k} (silhouette {sc:.3f})")
        return k, lab, sc
    k = int(k)
    lab = KMeans(k, n_init=10, random_state=SEED).fit_predict(Z)
    return k, lab, silhouette_score(Z, lab, sample_size=min(len(Z), 5000), random_state=SEED)


TRAITS = {  # (phrase when the archetype sits well above the population, phrase when well below)
    "log_n_swaps": ("very active", "light activity"), "log_median_gap": ("slow cadence", "rapid cadence"),
    "gap_cv": ("irregular gaps", "metronomic"), "log_min_gap": ("never sub-second", "sub-second bursts"),
    "fast_share": ("bursty", "no bursts"), "hour_entropy": ("around the clock", "concentrated hours"),
    "weekend_share": ("trades weekends", "weekdays only"), "us_hours_share": ("US-hours trader", "outside US hours"),
    "log_n_pools": ("many pools", "few pools"), "top_pool_share": ("one dominant pool", "spread across pools"),
    "stock_share": ("some stock pools", "no stock pools"), "stable_share": ("some stable pairs", "no stable pairs"),
    "log_n_tokens": ("many tokens", "few tokens"), "round_amount_frac": ("round sizes", "computed sizes"),
    "repeat_amount_frac": ("repeated sizes", "varied sizes"), "log_size_median": ("large tickets", "small tickets"),
    "log_size_iqr": ("varied ticket sizes", "uniform ticket sizes"), "reactive_share": ("reactive", "not reactive"),
    "round_trips_per_100": ("fast round trips", "no round trips"), "net_flow_frac": ("one-directional flow", "no net position"),
    "multi_pool_per_100": ("multi-pool blocks", "one pool per block")}


def words(c, z):
    """Plain-word label and description from an archetype's median raw features (c) and its median standardised
    vector (z): the label names the cadence and the three traits where it differs most from the population."""
    g = c["median_gap"]
    cad = "sub-second" if g < 1 else "seconds-apart" if g < 60 else "minutes-apart" if g < 3600 else "hours-apart"
    cadence = cad + " cadence" + (", metronomic" if c["gap_cv"] < 0.5 else ", bursty" if c["fast_share"] > 0.5 else "")
    n = c["n_pools"]
    pools = "single pool" if n <= 1.5 else "few pools" if n <= 5 else "many pools"
    if c["stock_share"] > 0.5: pools += ", mostly stock pools"
    elif c["stable_share"] > 0.5: pools += ", mostly stable pairs"
    sizing = "round sizes" if c["round_amount_frac"] > 0.5 else "repeated sizes" if c["repeat_amount_frac"] > 0.5 \
        else "computed sizes" if c["round_amount_frac"] < 0.05 else "mixed sizes"
    s = int(c["busiest_block_start"]); e = (s + 4) % 24
    if c["hour_entropy"] > 0.9 * math.log(24): timing = "around the clock"
    elif 12 <= s <= 15: timing = "trades around the US open"
    elif 18 <= s <= 21: timing = "trades around the US close"
    else: timing = f"busiest {s:02d}-{e:02d} UTC"
    if c["weekend_share"] < 0.05: timing += ", weekdays only"
    beh = [w for w, ok in [("reactive", c["reactive_share"] >= 0.5), ("fast round trips", c["round_trips_per_100"] >= 10),
                           ("no net position", c["net_flow_frac"] < 0.1), ("multi-pool blocks", c["multi_pool_per_100"] >= 10)] if ok]
    top = [d for d in sorted(VECTOR, key=lambda d: -abs(z[d])) if abs(z[d]) >= 0.5 and d != "log_median_gap"][:3]
    label = ", ".join([cadence.split(",")[0]] + [TRAITS[d][0 if z[d] > 0 else 1] for d in top] + ([timing.split(",")[0]] if len(top) < 3 else []))
    desc = (f"{cadence} (median gap {g:,.1f} s, gap CV {c['gap_cv']:.2f}, {c['fast_share']:.0%} of swaps within 2 s of the last); "
            f"{pools} ({n:.0f} pools, {c['n_tokens']:.0f} tokens, top pool {c['top_pool_share']:.0%} of swaps, "
            f"stock {c['stock_share']:.0%}, stable pairs {c['stable_share']:.0%}); {sizing} ({c['round_amount_frac']:.0%} round, "
            f"{c['repeat_amount_frac']:.0%} repeated); {timing} (hour entropy {c['hour_entropy']:.2f} of {math.log(24):.2f}, "
            f"busiest block {s:02d}-{e:02d} UTC holds {c['busiest_block_share']:.0%}, weekend {c['weekend_share']:.0%}); "
            f"reactive {c['reactive_share']:.0%}, {c['round_trips_per_100']:.1f} round trips and {c['multi_pool_per_100']:.1f} "
            f"multi-pool blocks per 100 swaps, net flow {c['net_flow_frac']:.2f}; about {c['n_swaps']:.0f} swaps a week.")
    return label, desc


def nearest(Z, df, agent_info, top=3):
    """Top-3 registered agents by cosine distance in the standardised space, per wallet (self excluded)."""
    from sklearn.metrics.pairwise import cosine_distances
    idx = np.flatnonzero(df.is_registered_agent.to_numpy())
    if len(idx) == 0: return [None] * len(df)
    Dm = cosine_distances(Z, Z[idx])
    out = []
    for i in range(len(df)):
        row = Dm[i]
        order = [j for j in np.argsort(row, kind="stable") if idx[j] != i][:top]
        out.append([{"wallet": df.w.iat[idx[j]], **agent_info.get(df.w.iat[idx[j]], {}), "distance": round(float(row[j]), 4)} for j in order])
    return out


def main():
    t0 = time.time(); timings = {}
    with psycopg.connect(DB) as conn:
        conn.execute("SET work_mem = '256MB'"); conn.execute("SET statement_timeout = '10min'")
        end = conn.execute("SELECT now()").fetchone()[0].astimezone(timezone.utc); start = end - timedelta(hours=HOURS)
        base = pd.read_sql("""SELECT wallet, is_automated, is_registered_agent, signals, reactive_swaps, n_swaps AS feat_swaps
                              FROM wallet_features WHERE (is_automated OR is_registered_agent) AND n_swaps >= %s""", conn, params=(MIN_SWAPS,))
        base["reactive_share"] = base.reactive_swaps / base.feat_swaps.clip(lower=1)
        log(f"{len(base):,} candidate wallets ({int(base.is_automated.sum()):,} automated, {int(base.is_registered_agent.sum()):,} registered)")
        t = time.time(); df = profiles(conn, base.wallet, start, end); timings["profiles"] = time.time() - t
        log(f"{len(df):,} wallets with >= {MIN_SWAPS} swaps in the window, {int(df.n_swaps.sum()):,} swaps, in {timings['profiles']:.0f}s "
            f"(known-pool share of swaps {np.average(df.known_pool_share, weights=df.n_swaps):.0%})")
        fam = pd.read_sql("SELECT wallet AS w, cluster_id AS family_id, size AS family_size FROM wallet_clusters WHERE window_end = (SELECT max(window_end) FROM wallet_clusters)", conn)
        agents = pd.read_sql("""SELECT DISTINCT ON (w) w, agent_id, profile->>'name' AS name FROM (
                                  SELECT f.wallet AS w, a.agent_id, a.profile FROM wallet_features f JOIN agents a
                                  ON a.agent_wallet = f.wallet OR a.owner = f.wallet OR a.current_owner = f.wallet
                                  WHERE f.is_registered_agent AND f.n_swaps >= %s) x
                                ORDER BY w, (profile->>'name') IS NULL, agent_id""", conn, params=(MIN_SWAPS,))
        if STABILITY:
            t = time.time(); halves = profiles(conn, base.wallet, start, end, mid=start + timedelta(hours=HOURS / 2)); timings["halves"] = time.time() - t
            log(f"stability: {int((halves.bucket == 0).sum()):,} / {int((halves.bucket == 1).sum()):,} wallets in the two halves, in {timings['halves']:.0f}s")
    df = df.merge(base.rename(columns={"wallet": "w"})[["w", "is_automated", "is_registered_agent", "signals", "reactive_share"]], on="w")
    df = df.merge(fam, on="w", how="left").sort_values("w").reset_index(drop=True)
    agent_info = {r.w: {"agent_id": int(r.agent_id), "name": r.name} for r in agents.itertuples()}

    t = time.time(); Z = standardise(df); k, lab, sil = cluster(Z, K); timings["cluster"] = time.time() - t
    df["archetype_id"] = lab; Zf = pd.DataFrame(Z, columns=VECTOR)
    log(f"clustered {len(df):,} wallets into {k} archetypes in {timings['cluster']:.0f}s")
    arch = []
    for a in range(k):
        m = df[df.archetype_id == a]
        cen = {c: float(m[c].median()) if m[c].notna().any() else None for c in RAW}
        label, desc = words({c: (v if v is not None else 0.0) for c, v in cen.items()}, Zf[df.archetype_id == a].median())
        arch.append(dict(archetype_id=a, n_wallets=len(m), label=label, description=desc, centroid=cen,
                         n_families=int(m.family_id.nunique()), n_registered=int(m.is_registered_agent.sum()),
                         example_wallets=list(m.sort_values("n_swaps", ascending=False).w.head(5)),
                         registered_agent_names=sorted({agent_info[w]["name"] for w in m[m.is_registered_agent].w if agent_info.get(w, {}).get("name")})))
        log(f"  archetype {a}: {len(m):,} wallets, {arch[-1]['n_families']} families, {arch[-1]['n_registered']} registered: {label}")
    df["archetype_label"] = df.archetype_id.map({a["archetype_id"]: a["label"] for a in arch})
    df["nearest_agents"] = nearest(Z, df, agent_info)
    df["features"] = [dict(zip(VECTOR, (round(float(v), 3) for v in z))) for z in Z]
    df["period_s"] = np.where(df.gap_cv < 0.5, df.median_gap, np.nan)

    ari = None
    if STABILITY:
        from sklearn.metrics import adjusted_rand_score
        hv = halves.merge(base.rename(columns={"wallet": "w"})[["w", "reactive_share"]], on="w")
        parts = {}
        for b in (0, 1):
            h = hv[hv.bucket == b].sort_values("w").reset_index(drop=True)
            parts[b] = pd.Series(cluster(standardise(h), k)[1], index=h.w)
        both = parts[0].index.intersection(parts[1].index)
        ari = adjusted_rand_score(parts[0][both], parts[1][both]) if len(both) else float("nan")
        log(f"stability: adjusted Rand index {ari:.3f} between halves on {len(both):,} wallets present in both (k={k})")

    t = time.time()
    cols = ["w", "window_start", "window_end", "n_swaps", "is_automated", "is_registered_agent", "signals",
            "median_gap", "gap_cv", "min_gap", "period_s", "fast_share", "hour_hist", "weekday_hist", "hour_entropy",
            "busiest_block_start", "busiest_block_share", "weekend_share", "us_hours_share", "n_pools", "top_pool_share",
            "stock_share", "stable_share", "other_share", "known_pool_share", "n_tokens", "round_amount_frac", "repeat_amount_frac",
            "log_size_median", "log_size_iqr", "reactive_share", "round_trips_per_100", "net_flow_frac", "multi_pool_per_100",
            "archetype_id", "archetype_label", "family_id", "family_size", "nearest_agents", "features"]
    df["window_start"] = start; df["window_end"] = end
    def val(c, v):
        if c in ("nearest_agents", "features"): return Jsonb(v)
        if isinstance(v, (list, str)): return v
        if v is None or pd.isna(v): return None
        if isinstance(v, (bool, np.bool_)): return bool(v)
        if isinstance(v, (np.floating, float)): return float(v)
        if isinstance(v, (np.integer, int)): return int(v)
        return v
    rows = [tuple(val(c, r[c]) for c in cols) for _, r in df[cols].iterrows()]
    names = ["wallet"] + cols[1:]
    with psycopg.connect(DB) as conn, conn.cursor() as cur:
        cur.executemany(f"INSERT INTO wallet_fingerprints ({', '.join(names)}, computed_at) VALUES ({', '.join('%s' for _ in names)}, now()) "
                        f"ON CONFLICT (wallet) DO UPDATE SET {', '.join(f'{c} = EXCLUDED.{c}' for c in names[1:])}, computed_at = now()", rows)
        cur.execute("DELETE FROM wallet_fingerprints WHERE window_end < %s", (end,))      # wallets that dropped out of the set
        cur.execute("DELETE FROM fingerprint_archetypes")
        cur.executemany("INSERT INTO fingerprint_archetypes (archetype_id, n_wallets, label, description, centroid, example_wallets, registered_agent_names) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        [(a["archetype_id"], a["n_wallets"], a["label"], a["description"], Jsonb(a["centroid"]), a["example_wallets"], a["registered_agent_names"]) for a in arch])
        conn.commit()
    timings["write"] = time.time() - t
    log(f"stored {len(rows):,} fingerprints and {k} archetypes in {timings['write']:.0f}s; total {time.time()-t0:.0f}s")

    if REPORT:
        reg = df[df.is_registered_agent]; named = [w for w in reg.w if agent_info.get(w, {}).get("name")]
        L = [f"# Wallet fingerprints\n\nWindow {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC ({HOURS:.0f} h); wallets labelled automated or "
             f"registered with at least {MIN_SWAPS} swaps in the window. {len(df):,} wallets, {int(df.n_swaps.sum()):,} swaps, "
             f"{k} archetypes (k by silhouette over 4..12: {sil:.3f}).\n",
             "## Archetypes\n", "| id | label | wallets | families | registered agents |", "|---|---|---|---|---|"]
        L += [f"| {a['archetype_id']} | {a['label']} | {a['n_wallets']:,} | {a['n_families']} | {a['n_registered']}" +
              (f" ({', '.join(a['registered_agent_names'])})" if a["registered_agent_names"] else "") + " |" for a in arch]
        L += ["", "Descriptions:", ""] + [f"- **{a['archetype_id']}** {a['description']}" for a in arch]
        L += ["", "## Stability", "",
              (f"Clustering the two halves of the window separately (k={k}) gives an adjusted Rand index of **{ari:.3f}** "
               f"on the wallets present in both halves. 1.0 would be identical partitions, 0 is chance." if ari is not None
               else "Not run (pass --stability)."),
              "", "## Registered-agent coverage", "",
              f"- {int(base.is_registered_agent.sum())} registered-agent wallets had at least {MIN_SWAPS} swaps in wallet_features; "
              f"{len(reg)} of them traded at least {MIN_SWAPS} times in this window and carry a fingerprint; {int(reg.is_automated.sum())} of those also carry the automated label.",
              f"- {len(named)} of those wallets have a profile name in the registry ({len(agent_info)} registered wallets in the set in all). "
              "Every wallet's `nearest_agents` lists the three closest registered agents by cosine distance in the standardised feature space.",
              "", "## Caveats", "",
              "- An archetype is a cluster of behaviour, not a model. No registry profile names a model or framework, so nothing here says what a wallet *is*; it says what it behaves like.",
              "- k-means with robust scaling finds compact blobs in a 21-dimensional space; the boundaries between neighbouring archetypes are soft and a wallet near one can switch between runs. The stability index above is the honest measure of that.",
              f"- Pool token metadata is incomplete: {np.average(df.known_pool_share, weights=df.n_swaps):.0%} of the swaps here are in pools whose tokens are known. Venue shares (stock, stable, other) are over all swaps, so they under-count when a wallet trades unknown pools; `known_pool_share` says how much.",
              "- Trade size is only measured where token1 is a stable coin (`log_size_median`, `log_size_iqr`); elsewhere it is imputed with the population median before clustering.",
              "- `reactive_share` comes from the labeller's wallet_features window, not from this window, because it needs every other wallet's swaps in the pool; in the stability check it is the same number in both halves.",
              "- Registered agents are few and mostly not automated by our labels; the nearest-agent lists are therefore rarely informative and should be read as 'closest of a small set'.",
              "- Labels name the cadence and the three traits where the archetype sits furthest from the population median, so 'large tickets' or 'some stock pools' are relative to this population, not absolute; the description carries the numbers."]
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
        with open(REPORT_PATH, "w") as f: f.write("\n".join(L) + "\n")
        log(f"report written to {os.path.relpath(REPORT_PATH, os.path.join(os.path.dirname(__file__), '..'))}")


if __name__ == "__main__":
    main()
