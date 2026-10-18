"""Fee-revenue simulation for the crowding-aware hook, on the per-minute aggregates.

Usage: python scripts/hook_fee_sim.py [--from ISO --to ISO] [--table pool_minutes|pool_minutes_all]
       [--base-bp 30] [--herd 15] [--surcharge-bp 20] [--rebate-bp 10] [--horizon 5] [--out reports/hook_fee_sim.md]

Only pools with a stablecoin on one side are priced (that side's amount is the dollar volume; stable-as-token0 pools are flipped). Three fee schedules
per minute and side: flat (base), crowding-adjusted (herd side pays base + surcharge, the leaning-against
side pays base - rebate, calm minutes pay base), and the same judged one minute late (what an on-chain score
posted each minute would give the hook). The adverse-selection proxy is the signed price move over the next
`horizon` minutes after a herd minute, times the herd-side volume: what LPs lose to the herd at a flat fee.
"""
import argparse, os, sys, time
from datetime import datetime, timedelta, timezone
import numpy as np, pandas as pd, psycopg

DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
def log(*a): print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)

ap = argparse.ArgumentParser()
ap.add_argument("--from", dest="from_"); ap.add_argument("--to")
ap.add_argument("--table", default="pool_minutes")
ap.add_argument("--base-bp", type=float, default=30); ap.add_argument("--herd", type=int, default=15)
ap.add_argument("--surcharge-bp", type=float, default=20); ap.add_argument("--rebate-bp", type=float, default=10)
ap.add_argument("--opp-share", type=float, default=0.15, help="other side at most this share of the herd side")
ap.add_argument("--horizon", type=int, default=5); ap.add_argument("--out", default="reports/hook_fee_sim.md")
a = ap.parse_args()
to = datetime.fromisoformat(a.to).replace(tzinfo=timezone.utc) if a.to else datetime.now(timezone.utc)
frm = datetime.fromisoformat(a.from_).replace(tzinfo=timezone.utc) if a.from_ else to - timedelta(days=7)

# Pools are priced from whichever side is the stablecoin. When the stable is token1 the rows are used as stored; when it is
# token0 (USDG's address sorts below most tokens, so this is the larger half) the pool is flipped so that "token0" below
# always means the non-stable token: sides swap, the price inverts, and the dollar volume is the stable side's amount.
# Copycat-quoted pools are dropped: zero-fee v4 pools pairing USDG with a fake "USDG" printed $500M each in a week by
# round-tripping inside one transaction (v4 flash accounting settles the net, which is nothing). Static zero-fee pools are
# dropped for the same reason: a round trip there costs nothing, and in the live week they were almost entirely such trips.
SQL = f"""
SELECT m.pool, m.minute, m.direction, m.swaps, m.auto_wallets, m.auto_swaps,
       m.amount1_in / power(10, t1.decimals) AS usd, m.px_median, p.fee,
       coalesce(t0.kind, 'other') AS kind0, coalesce(t0.symbol, left(p.token0, 10)) AS sym0
FROM {a.table} m
JOIN pools p ON p.pool = m.pool
JOIN tokens t1 ON t1.token = p.token1 AND t1.kind = 'stable'
LEFT JOIN tokens t0 ON t0.token = p.token0
WHERE coalesce(t0.kind, 'other') NOT IN ('stable', 'copycat') AND p.fee <> 0 AND m.minute >= %(frm)s AND m.minute < %(to)s AND m.px_median IS NOT NULL AND m.px_median > 0
UNION ALL
SELECT m.pool, m.minute, CASE m.direction WHEN 'buy0' THEN 'sell0' ELSE 'buy0' END, m.swaps, m.auto_wallets, m.auto_swaps,
       m.amount0_in / power(10, t0.decimals) AS usd, 1 / m.px_median, p.fee,
       coalesce(t1.kind, 'other') AS kind0, coalesce(t1.symbol, left(p.token1, 10)) AS sym0
FROM {a.table} m
JOIN pools p ON p.pool = m.pool
JOIN tokens t0 ON t0.token = p.token0 AND t0.kind = 'stable'
LEFT JOIN tokens t1 ON t1.token = p.token1
WHERE coalesce(t1.kind, 'other') NOT IN ('stable', 'copycat') AND p.fee <> 0 AND m.minute >= %(frm)s AND m.minute < %(to)s AND m.px_median IS NOT NULL AND m.px_median > 0
"""
t = time.time()
with psycopg.connect(DB) as conn:
    conn.execute("SET work_mem = '256MB'"); conn.execute("SET statement_timeout = '15min'")
    df = pd.read_sql(SQL, conn, params={"frm": frm, "to": to})
log(f"{len(df):,} pool-minute-sides, {df.pool.nunique():,} stable-quoted pools, ${df.usd.sum():,.0f} volume ({time.time()-t:.0f}s)")
if df.empty: sys.exit("nothing to price")

# one row per pool-minute with both sides
w = df.pivot_table(index=["pool", "minute"], columns="direction", values=["usd", "auto_wallets", "swaps"], aggfunc="sum", fill_value=0)
w.columns = [f"{v}_{d}" for v, d in w.columns]
for c in ["usd_buy0", "usd_sell0", "auto_wallets_buy0", "auto_wallets_sell0", "swaps_buy0", "swaps_sell0"]:
    if c not in w: w[c] = 0
w = w.reset_index()
px = df.groupby(["pool", "minute"]).px_median.median().rename("px").reset_index()
w = w.merge(px, on=["pool", "minute"]).merge(df.groupby("pool").agg(fee=("fee", "first"), kind0=("kind0", "first"), sym0=("sym0", "first")).reset_index(), on="pool")
w = w.sort_values(["pool", "minute"]).reset_index(drop=True)

def herd_side(ab, asl):
    if ab >= a.herd and asl <= a.opp_share * ab: return "buy0"
    if asl >= a.herd and ab <= a.opp_share * asl: return "sell0"
    return None
w["herd"] = [herd_side(b, s) for b, s in zip(w.auto_wallets_buy0, w.auto_wallets_sell0)]
# the posted-score variant: the hook sees the previous minute's count for this pool
g = w.groupby("pool")
w["prev_minute"] = g.minute.shift(1); w["prev_herd"] = g.herd.shift(1)
w["herd_lag"] = np.where((w.minute - w.prev_minute) <= pd.Timedelta(minutes=1), w.prev_herd, None)

# forward price move over the horizon (nearest later minute within 2x horizon), signed in the herd's direction
w["t_fwd"] = w.minute + pd.Timedelta(minutes=a.horizon)
fwd = pd.merge_asof(w[["pool", "t_fwd"]].reset_index().sort_values("t_fwd"), w[["pool", "minute", "px"]].rename(columns={"minute": "m_fwd", "px": "px_fwd"}).sort_values("m_fwd"),
                    left_on="t_fwd", right_on="m_fwd", by="pool", direction="forward", tolerance=pd.Timedelta(minutes=2 * a.horizon))
fwd = fwd.set_index("index").sort_index()   # merge_asof returns rows in key order; restore the original row order
w = w.merge(fwd.drop(columns=["t_fwd", "pool"]), left_index=True, right_index=True, how="left")
w["move_bp"] = 1e4 * (w.px_fwd / w.px - 1)   # px = token1 per token0; buy0 herd pushes it up
w["herd_move_bp"] = np.where(w.herd == "buy0", w.move_bp, np.where(w.herd == "sell0", -w.move_bp, np.nan))

def revenue(frame, col):
    """fees in dollars under flat and adjusted schedules; adjusted judged by the herd column `col`"""
    base, sur, reb = a.base_bp / 1e4, a.surcharge_bp / 1e4, a.rebate_bp / 1e4
    h = frame[col]
    buy_fee = np.where(h == "buy0", base + sur, np.where(h == "sell0", base - reb, base))
    sell_fee = np.where(h == "sell0", base + sur, np.where(h == "buy0", base - reb, base))
    flat = base * (frame.usd_buy0 + frame.usd_sell0)
    adj = buy_fee * frame.usd_buy0 + sell_fee * frame.usd_sell0
    herd_vol = np.where(h == "buy0", frame.usd_buy0, np.where(h == "sell0", frame.usd_sell0, 0.0))
    lean_vol = np.where(h == "buy0", frame.usd_sell0, np.where(h == "sell0", frame.usd_buy0, 0.0))
    return dict(flat=flat.sum(), adjusted=adj.sum(), surcharge=(sur * herd_vol).sum(), rebate=(reb * lean_vol).sum(),
                herd_vol=herd_vol.sum(), lean_vol=lean_vol.sum(), herd_minutes=int((h.notna()).sum()))

tot_vol = w.usd_buy0.sum() + w.usd_sell0.sum()
R = revenue(w, "herd"); RL = revenue(w, "herd_lag")
hm = w[w.herd.notna()]
adverse = (hm.herd_move_bp.clip(-500, 500) / 1e4 * np.where(hm.herd == "buy0", hm.usd_buy0, hm.usd_sell0)).sum()
move_med = hm.herd_move_bp.median(); move_mean = hm.herd_move_bp.clip(-500, 500).mean()
calm = w[w.herd.isna() & w.px_fwd.notna()]
calm_move = (calm.move_bp.abs()).median()

# sensitivity over surcharge/rebate
sens = []
for sur in [0, 10, 20, 30, 50]:
    for reb in [0, 5, 10, 15]:
        a.surcharge_bp, a.rebate_bp = sur, reb; r = revenue(w, "herd")
        sens.append((sur, reb, r["adjusted"] / r["flat"] - 1))
a.surcharge_bp, a.rebate_bp = 20, 10

# pools that would earn most
w["adj_rev"] = np.nan
base, sur, reb = a.base_bp / 1e4, a.surcharge_bp / 1e4, a.rebate_bp / 1e4
w["adj_rev"] = (np.where(w.herd == "buy0", base + sur, np.where(w.herd == "sell0", base - reb, base)) * w.usd_buy0
                + np.where(w.herd == "sell0", base + sur, np.where(w.herd == "buy0", base - reb, base)) * w.usd_sell0)
w["flat_rev"] = base * (w.usd_buy0 + w.usd_sell0)
w["herd_usd"] = np.where(w.herd == "buy0", w.usd_buy0, np.where(w.herd == "sell0", w.usd_sell0, 0.0))
top = w.groupby(["pool", "sym0", "kind0", "fee"], dropna=False).agg(volume=("flat_rev", lambda s: s.sum() / base), flat=("flat_rev", "sum"), adjusted=("adj_rev", "sum"),
                                                     herd_minutes=("herd", lambda s: s.notna().sum()), herd_usd=("herd_usd", "sum")).reset_index().sort_values("adjusted", ascending=False).head(12)
dyn = w[w.fee == 8388608]; dyn_share = (dyn.usd_buy0.sum() + dyn.usd_sell0.sum()) / tot_vol
by_kind = w.groupby("kind0").apply(lambda f: pd.Series(dict(volume=f.usd_buy0.sum() + f.usd_sell0.sum(), herd_usd=f.herd_usd.sum(), herd_minutes=f.herd.notna().sum()))).reset_index()

L = [f"# Hook fee simulation", "",
     f"Window {frm:%Y-%m-%d %H:%M} to {to:%Y-%m-%d %H:%M} UTC, table `{a.table}`. {w.pool.nunique():,} pools quoted in a stablecoin, "
     f"{len(w):,} pool-minutes, ${tot_vol:,.0f} volume. Herd = {a.herd}+ automated wallets one way with the other side at most {a.opp_share:.0%} of it. "
     f"Schedule: base {a.base_bp:.0f} bp; herd side {a.base_bp:.0f}+{a.surcharge_bp:.0f} bp; leaning-against side {a.base_bp:.0f}-{a.rebate_bp:.0f} bp.", "",
     "## Headline", "",
     f"| | flat {a.base_bp:.0f} bp | crowding-adjusted | adjusted, judged one minute late |", "|---|---|---|---|",
     f"| Fee revenue | ${R['flat']:,.0f} | ${R['adjusted']:,.0f} ({R['adjusted']/R['flat']-1:+.1%}) | ${RL['adjusted']:,.0f} ({RL['adjusted']/R['flat']-1:+.1%}) |",
     f"| Surcharge collected | – | ${R['surcharge']:,.0f} | ${RL['surcharge']:,.0f} |",
     f"| Rebates paid | – | ${R['rebate']:,.0f} | ${RL['rebate']:,.0f} |",
     f"| Herd minutes | – | {R['herd_minutes']:,} | {RL['herd_minutes']:,} |",
     f"| Herd-side volume | – | ${R['herd_vol']:,.0f} ({R['herd_vol']/tot_vol:.1%} of all) | ${RL['herd_vol']:,.0f} |",
     f"| Leaning-against volume | – | ${R['lean_vol']:,.0f} | ${RL['lean_vol']:,.0f} |", "",
     "## What the herd costs LPs at a flat fee", "",
     f"- Signed price move over the next {a.horizon} minutes after a herd minute, in the herd's direction: median {move_med:+.1f} bp, mean {move_mean:+.1f} bp (clipped at ±500). Calm-minute absolute move for comparison: median {calm_move:.1f} bp.",
     f"- Adverse selection proxy (move × herd-side volume): ${adverse:,.0f} over the window, against ${base*R['herd_vol']:,.0f} of flat fees earned on that same volume.",
     f"- So in herd minutes LPs {'lose more to the move than they earn in fees' if adverse > base*R['herd_vol'] else 'still earn more in fees than the move costs them'} at a flat {a.base_bp:.0f} bp; the surcharge of {a.surcharge_bp:.0f} bp recovers ${R['surcharge']:,.0f} of it.", "",
     "## Sensitivity (revenue change vs flat)", "", "| surcharge \\ rebate | " + " | ".join(f"{r} bp" for r in [0, 5, 10, 15]) + " |", "|---|---|---|---|---|"]
for sur in [0, 10, 20, 30, 50]:
    L.append(f"| {sur} bp | " + " | ".join(f"{d:+.2%}" for s, r, d in sens if s == sur) + " |")
L += ["", "## Pools that would earn most under the adjusted schedule", "", "| pool | token0 | kind | fee tier | volume | flat | adjusted | herd minutes | herd volume |", "|---|---|---|---|---|---|---|---|---|"]
for r in top.itertuples():
    L.append(f"| {r.pool[:10]}… | {r.sym0 or '?'} | {r.kind0} | {'dynamic' if r.fee == 8388608 else f'{r.fee/1e4:.2f}%'} | ${r.volume:,.0f} | ${r.flat:,.0f} | ${r.adjusted:,.0f} | {r.herd_minutes} | ${r.herd_usd:,.0f} |")
L += ["", "## By token kind", "", "| kind | volume | herd volume | herd minutes |", "|---|---|---|---|"]
for r in by_kind.itertuples():
    L.append(f"| {r.kind0} | ${r.volume:,.0f} | ${r.herd_usd:,.0f} | {int(r.herd_minutes):,} |")
L += ["", f"Pools already on the v4 dynamic-fee flag carry {dyn_share:.0%} of stable-quoted volume: a dynamic fee is normal on this chain, the hook only changes what drives it.", "",
      "## Caveats", "", "- Volume is counted from pool minutes, so a swap's fee is applied to the whole minute's volume on its side; a hook sees each swap and the herd count as it stands at that block, which lies between the two columns above.",
      "- The schedule is applied to the volume that actually traded; a surcharge changes behaviour (some herd flow goes elsewhere, some rebated flow arrives), which this does not model. Treat the revenue change as an upper bound and the LP-loss figure as the reason to adopt.",
      "- Only stable-quoted pools are priced because the dollar volume is unambiguous there; herding in token-token pools is excluded."]
os.makedirs(os.path.dirname(a.out), exist_ok=True); open(a.out, "w").write("\n".join(L) + "\n")
log(f"wrote {a.out}: flat ${R['flat']:,.0f}, adjusted ${R['adjusted']:,.0f} ({R['adjusted']/R['flat']-1:+.1%}), herd volume {R['herd_vol']/tot_vol:.1%}, adverse ${adverse:,.0f}")
