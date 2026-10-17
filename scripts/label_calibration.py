"""Calibration report for the behavioural labels: how each signal fires, how they overlap, how the
registered agents score, what the volume-bot rule catches, and what moving the thresholds would do.
Reads wallet_features only. Writes reports/label_calibration.md.

Usage: python scripts/label_calibration.py
"""
import os, sys, time, tomllib
import psycopg
import numpy as np, pandas as pd

DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cfg = tomllib.load(open(os.path.join(ROOT, "config", "labels.toml"), "rb"))
OUT = os.path.join(ROOT, "reports", "label_calibration.md")

def md(df, floatfmt="{:.2f}"):
    cols = list(df.columns)
    out = ["| " + " | ".join([str(df.index.name or "")] + cols) + " |", "|" + "---|" * (len(cols) + 1)]
    for idx, r in df.iterrows():
        vals = [(f"{v:,}" if isinstance(v, (int, np.integer)) else (floatfmt.format(v) if isinstance(v, (float, np.floating)) else str(v))) for v in r.values]
        out.append("| " + " | ".join([str(idx)] + vals) + " |")
    return "\n".join(out)

with psycopg.connect(DB) as conn:
    import warnings; warnings.filterwarnings("ignore")
    w = pd.read_sql("SELECT * FROM wallet_features", conn)
    reg = pd.read_sql("SELECT agent_wallet, owner, current_owner FROM agents", conn)
    cw = pd.read_sql("SELECT wallet, size FROM wallet_clusters WHERE window_end = (SELECT max(window_end) FROM wallet_clusters)", conn)
w["window_hours"] = (pd.to_datetime(w.window_end) - pd.to_datetime(w.window_start)).dt.total_seconds() / 3600
judged = w[w.n_swaps >= cfg["signals"]["min_swaps_to_label"]].copy()
sig_names = ["same_block_multi_pool", "sub_second_gap", "metronomic", "fast_round_trips", "computed_sizes", "reactive", "persistent"]
for s in sig_names: judged[s] = judged.signals.apply(lambda a: s in (a or []))
L = ["# Label calibration\n", f"{len(w):,} wallets in wallet_features, {len(judged):,} judged (>= {cfg['signals']['min_swaps_to_label']} swaps), "
     f"{int(judged.is_automated.sum()):,} automated ({judged.is_automated.mean():.0%} of judged), {int(w.is_volume_bot.sum()):,} volume bots. "
     f"Feature windows: median {w.window_hours.median():.1f} h, max {w.window_hours.max():.0f} h.\n"]
# 1. signal frequencies and what each contributes
rows = []
for s in sig_names:
    f = judged[s]
    rows.append({"signal": s, "wallets": int(f.sum()), "share_of_judged": f.mean(), "share_of_automated": judged.loc[judged.is_automated, s].mean(),
                 "automated_if_fires": judged.loc[f, "is_automated"].mean() if f.any() else np.nan,
                 "alone_would_drop": int(((judged.n_signals == cfg["signals"]["min_signals"]) & f & judged.is_automated).sum())})
L.append("## Signals\n"); L.append("`alone_would_drop` = automated wallets sitting exactly on the threshold that this signal carries.\n")
L.append(md(pd.DataFrame(rows).set_index("signal")))
# 2. pairwise overlap (Jaccard)
J = pd.DataFrame(index=sig_names, columns=sig_names, dtype=float)
for a in sig_names:
    for b in sig_names:
        A, B = judged[a], judged[b]; J.loc[a, b] = (A & B).sum() / max((A | B).sum(), 1)
L.append("\n## Overlap between signals (Jaccard)\n"); L.append(md(J.round(2)))
# 3. threshold sweep
rows = []
for k in range(1, 6):
    auto = judged.n_signals >= k
    rows.append({"min_signals": k, "automated": int(auto.sum()), "share_of_judged": auto.mean(), "swaps_covered": int(judged.loc[auto, "n_swaps"].sum()), "share_of_judged_swaps": judged.loc[auto, "n_swaps"].sum() / judged.n_swaps.sum()})
L.append("\n## What the automated threshold does (currently min_signals = %d)\n" % cfg["signals"]["min_signals"]); L.append(md(pd.DataFrame(rows).set_index("min_signals")))
# 4. registered agents
regw = set(reg.agent_wallet.dropna()) | set(reg.owner.dropna()) | set(reg.current_owner.dropna())
ra = w[w.wallet.isin(regw)]
L.append(f"\n## Registered ERC-8004 agents as a truth set\n\n{len(ra):,} registered-agent wallets have traded; {int((ra.n_swaps >= cfg['signals']['min_swaps_to_label']).sum()):,} judged; "
         f"{int(ra.is_automated.sum()):,} labelled automated. Signal counts among the judged ones: "
         f"{dict(ra[ra.n_swaps >= cfg['signals']['min_swaps_to_label']].n_signals.value_counts().sort_index())}. "
         "Registration is a weak truth set (most registered agents trade little or are human-driven), so treat this as a sanity check, not a target.\n")
# 5. families vs labels
fam = w[w.wallet.isin(cw.wallet)]
L.append(f"## Correlated families vs labels\n\n{len(fam):,} wallets sit in a correlated family (last pairs run); {int(fam.is_automated.sum()):,} are labelled automated, "
         f"{int((fam.n_swaps < cfg['signals']['min_swaps_to_label']).sum()):,} have too few swaps to judge, {int(((fam.n_swaps >= cfg['signals']['min_swaps_to_label']) & ~fam.is_automated).sum()):,} are judged but not automated. "
         "Family membership is itself strong evidence of automation: wallets that move with the same partners in a thousand minutes are not people. Candidate eighth signal.\n")
# 6. volume bots
vb = cfg["volume_bots"]
cand = w[(w.n_swaps >= vb["min_swaps"])]
L.append("## Volume-bot rule\n")
L.append(f"Of {len(cand):,} wallets with >= {vb['min_swaps']} swaps: net_flow_frac <= {vb['net_flow_frac_max']}: {int((cand.net_flow_frac <= vb['net_flow_frac_max']).sum()):,}; "
         f"top_pool_share >= {vb['top_pool_share_min']}: {int((cand.top_pool_share >= vb['top_pool_share_min']).sum()):,}; both: "
         f"{int(((cand.net_flow_frac <= vb['net_flow_frac_max']) & (cand.top_pool_share >= vb['top_pool_share_min'])).sum()):,}; "
         f"with >= 3 active days: {int((cand.active_days >= 3).sum()):,}, of which daily_volume_cv <= {vb['daily_volume_cv_max']}: {int(((cand.active_days >= 3) & (cand.daily_volume_cv <= vb['daily_volume_cv_max'])).sum()):,}. "
         f"Labelled volume bots: {int(w.is_volume_bot.sum()):,}, holding {int(w.loc[w.is_volume_bot, 'n_swaps'].sum()):,} swaps; {int((w.is_volume_bot & w.is_automated).sum()):,} of them also automated.\n")
q = cand.net_flow_frac.quantile([.05, .1, .25, .5, .75]).round(3).to_dict(); L.append(f"net_flow_frac quantiles among candidates: {q}\n")
q = cand.top_pool_share.quantile([.25, .5, .75, .9]).round(3).to_dict(); L.append(f"top_pool_share quantiles: {q}\n")
# 7. feature distributions automated vs not
feats = ["median_gap", "min_gap", "gap_cv", "round_amount_frac", "repeat_amount_frac", "n_pools", "n_swaps", "net_flow_frac", "top_pool_share"]
d = judged.groupby("is_automated")[feats].median().T; d.columns = ["not automated (median)", "automated (median)"]
L.append("## Feature medians, automated vs not (judged wallets)\n"); L.append(md(d.round(3)))
os.makedirs(os.path.dirname(OUT), exist_ok=True); open(OUT, "w").write("\n".join(L) + "\n")
print("wrote", OUT)
