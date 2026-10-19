"""Build and publish the daily Shepherd's Warning from the launch audit and the day's counts, with a short post for X.

Steps: run the launch audit for the last 24 hours, pull the day's chain figures, write a Markdown issue with front
matter (kind: daily), publish it to the site through publish_issue.py, and write <slug>.post.txt: a post of at most
280 characters that states the day's verdicts in plain words, for pasting into the Tethera X account.

Usage: python scripts/daily_warning.py [--date YYYY-MM-DD] [--hours 24] [--top 12] [--dry-run]
"""
import os, sys, re, time, argparse, subprocess
from datetime import datetime, timezone, timedelta
import psycopg

ROOT = os.path.join(os.path.dirname(__file__), "..")
DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
log = lambda *a: print(time.strftime("%m-%d %H:%M:%S"), *a, flush=True)
ap = argparse.ArgumentParser()
ap.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
ap.add_argument("--hours", type=float, default=24); ap.add_argument("--top", type=int, default=12); ap.add_argument("--dry-run", action="store_true")
a = ap.parse_args()
PY = os.path.join(ROOT, ".venv", "bin", "python")
audit_md = os.path.join(ROOT, "reports", f"launch_audit_{a.date}.md")

# 1. two audits for the window: the day's launches, and the most traded tokens
launch_md = os.path.join(ROOT, "reports", f"launch_audit_{a.date}_launches.md")
subprocess.run([PY, os.path.join(ROOT, "scripts", "launch_audit.py"), "--days", str(a.hours / 24), "--top", str(a.top), "--min-swaps", "300", "--new-only", "--out", launch_md], check=True)
subprocess.run([PY, os.path.join(ROOT, "scripts", "launch_audit.py"), "--days", str(a.hours / 24), "--top", str(a.top), "--min-swaps", "1000", "--out", audit_md], check=True)
def read_table(path):
    lines = open(path).read().splitlines()
    head = next((l for l in lines if l.startswith("| token")), None)
    if not head: return []
    cols = [c.strip() for c in head.strip().strip("|").split("|")]
    rows = []
    for l in lines:
        if l.startswith("| ") and not l.startswith("| token") and not l.startswith("|---"):
            cells = [c.strip() for c in l.strip().strip("|").split("|")]
            if len(cells) == len(cols): rows.append(dict(zip(cols, cells)))
    return rows
launches, runners = read_table(launch_md), read_table(audit_md)

# 2. the day's figures
since = datetime.now(timezone.utc) - timedelta(hours=a.hours)
with psycopg.connect(DB) as conn:
    conn.execute("SET work_mem = '256MB'")
    swaps, wallets, auto_swaps, sc = conn.execute("""
        SELECT count(*), count(DISTINCT tx_from), count(*) FILTER (WHERE is_automated AND NOT self_cancel), count(*) FILTER (WHERE self_cancel)
        FROM swaps WHERE block_time >= %s""", (since,)).fetchone()
    herds, peak = conn.execute("""
        WITH m AS (SELECT pm.pool, pm.minute, pm.direction, pm.auto_wallets AS same,
                          coalesce((SELECT o.auto_wallets FROM pool_minutes o WHERE o.pool = pm.pool AND o.minute = pm.minute AND o.direction <> pm.direction), 0) AS opp
                   FROM pool_minutes pm WHERE pm.minute >= %s AND pm.auto_wallets >= 15)
        SELECT count(*) FILTER (WHERE opp <= 0.15 * same), max(same) FILTER (WHERE opp <= 0.15 * same) FROM m""", (since,)).fetchone()
    printed = conn.execute("""
        SELECT coalesce(sum(CASE WHEN t0.kind = 'stable' THEN abs(s.amount0) / power(10, t0.decimals) WHEN t1.kind = 'stable' THEN abs(s.amount1) / power(10, t1.decimals) END), 0)
        FROM swaps s JOIN pools p ON p.pool = s.pool LEFT JOIN tokens t0 ON t0.token = p.token0 LEFT JOIN tokens t1 ON t1.token = p.token1
        WHERE s.self_cancel AND s.block_time >= %s AND (t0.symbol = 'USDG' OR t1.symbol = 'USDG')""", (since,)).fetchone()[0]

# 2b. launches on the Pons curve in the window (seen before they ever reach Uniswap)
with psycopg.connect(DB) as conn:
    pons = conn.execute("""
        WITH l AS (SELECT * FROM launches WHERE block_time >= %s),
             c AS (SELECT l.token, c.trader, c.side, c.quote_amount, c.block_time, l.block_time AS launched, l.deployer FROM l JOIN curve_trades c ON c.curve = l.curve),
             per AS (SELECT token, trader, sum(quote_amount) FILTER (WHERE side = 'buy') AS bought FROM c GROUP BY 1, 2),
             agg AS (SELECT c.token, count(*) AS trades, count(DISTINCT trader) AS buyers, sum(quote_amount) FILTER (WHERE side = 'buy') / 1e18 AS raised,
                            count(DISTINCT trader) FILTER (WHERE block_time < launched + interval '60 seconds') AS first_minute_buyers,
                            count(*) FILTER (WHERE trader = deployer) AS deployer_trades FROM c GROUP BY 1),
             top AS (SELECT token, max(bought) AS top1 FROM per GROUP BY 1)
        SELECT coalesce(t.symbol, left(l.token, 10)), to_char(l.block_time AT TIME ZONE 'UTC', 'HH24:MI'), l.graduated_time IS NOT NULL,
               coalesce(a.trades, 0), coalesce(a.buyers, 0), round(coalesce(a.raised, 0)::numeric, 2), coalesce(a.first_minute_buyers, 0), coalesce(a.deployer_trades, 0),
               CASE WHEN a.raised > 0 THEN round(100.0 * top.top1 / 1e18 / a.raised) END
        FROM l LEFT JOIN tokens t ON t.token = l.token LEFT JOIN agg a ON a.token = l.token LEFT JOIN top ON top.token = l.token
        ORDER BY coalesce(a.raised, 0) DESC LIMIT %s""", (since, a.top)).fetchall()
    n_launches, n_grad = conn.execute("SELECT count(*), count(*) FILTER (WHERE graduated_time IS NOT NULL) FROM launches WHERE block_time >= %s", (since,)).fetchone()

# 3. rows for the two tables
def row(r):
    v = r.get("verdict", "").strip("*")
    return [r["token"], r["first seen (UTC)"].replace(" (before window)", ""), r["swaps"], r["wallets"], r["automated swaps"], r["fresh wallets"], r["born together"], r["top-10 share"], r.get("born for this / nonce ≤ 2 (median nonce)", "–"), v]
launch_rows = [row(r) for r in launches]; runner_rows = [row(r) for r in runners]
verdicts = [(r[0], r[-1]) for r in launch_rows + runner_rows]

date_h = datetime.strptime(a.date, "%Y-%m-%d").strftime("%-d %B %Y")
title = f"What was real on Robinhood Chain, {date_h}"
lines = ["---", f"date: {a.date}", f"title: {title}",
         f"summary: {swaps:,} swaps by {wallets:,} wallets; {100*auto_swaps/max(swaps-sc,1):.0f}% by software; {herds or 0:,} herd minutes; ${printed/1e6:,.1f}M self-cancelling.",
         "kind: daily", "---",
         f"# The day in numbers",
         "", f"Twenty-four hours to {datetime.now(timezone.utc):%H:%M} UTC on {date_h}. Counted from every Uniswap swap on the chain; self-cancelling trades flagged and excluded from everything but their own line.", "",
         "| Measure | Value |", "|---|---|",
         f"| Swaps | {swaps:,} |", f"| Wallets | {wallets:,} |", f"| Swaps by automated wallets | {100*auto_swaps/max(swaps-sc,1):.0f}% |",
         f"| One-sided herd minutes (15+ automated wallets one way) | {herds or 0:,} |", f"| Largest herd | {peak or 0:,} automated wallets one way in one minute |",
         f"| Self-cancelling swaps | {sc:,} |", f"| Self-cancelling USDG volume | ${printed/1e6:,.1f}M |",
         "", "# Launched on Pons today", "",
         f"{n_launches:,} tokens were launched on the Pons curve in the last day; {n_grad:,} graduated to Uniswap. The busiest by ETH raised, before they reached a pool anywhere a Uniswap reader can see:", "",
         "| token | launched (UTC) | graduated | curve trades | buyers | ETH raised | buyers in first minute | deployer's own trades | top buyer share |", "|---|---|---|---|---|---|---|---|---|"]
lines += [f"| {r[0]} | {r[1]} | {'yes' if r[2] else 'no'} | {r[3]:,} | {r[4]:,} | {r[5]} | {r[6]} | {r[7]} | {str(r[8]) + '%' if r[8] is not None else '–'} |" for r in pons] or ["| (none) | | | | | | | | |"]
lines += ["", "# Launched today on Uniswap, audited", "",
         "Tokens whose first trade on Uniswap fell inside the day, ranked by swaps. Automated = the seven-day behavioural label. Fresh = the wallet's first swap in our data was in this token. Born together = the largest batch of fresh wallets that first appeared in the same ten minutes. Top-10 = the share of swaps from the ten busiest wallets. Born for this = wallets whose whole on-chain life fits inside this token, from their transaction count.", "",
         "| token | first seen (UTC) | swaps | wallets | automated | fresh wallets | born together | top-10 share | born for this / one-trade | verdict |", "|---|---|---|---|---|---|---|---|---|---|"]
lines += ["| " + " | ".join(r) + " |" for r in launch_rows] or ["| (no launch reached 300 swaps today) | | | | | | | | | |"]
lines += ["", "# Most traded, audited", "",
          "The day's busiest tokens, new or old. Fresh and born-together are shown only for tokens born inside the day.", "",
          "| token | first seen (UTC) | swaps | wallets | automated | fresh wallets | born together | top-10 share | born for this / one-trade | verdict |", "|---|---|---|---|---|---|---|---|---|---|"]
lines += ["| " + " | ".join(r) + " |" for r in runner_rows]
lines += ["", "# How to read a verdict", "",
          "- **organic**: mostly wallets with history, no single operator dominating, few bots. The rare one.",
          "- **mixed**: real traders and bots side by side; the usual state of a live pool.",
          "- **automated**: bots do most of the swaps. Normal for an old pool full of arbitrage; on a day-old token it means the bots arrived and the people did not.",
          "- **fresh-wallet driven**: thousands of wallets with no history, trading once, appearing in the same minutes. One operator with a script, until shown otherwise.",
          "- **printed**: the volume cancels itself inside single transactions and nothing was traded.",
          "", "Every row can be checked at `api.tetheralabs.com/v1/pool/{pool}` for the token's pools. Corrections to TetheraLabs@proton.me.", ""]
issue_path = os.path.join(ROOT, "reports", f"daily_{a.date}.md")
open(issue_path, "w").write("\n".join(lines))

# 4. the post
seen = set(); uniq = [v for v in verdicts if not (v[0] in seen or seen.add(v[0]))]
worst = [v for v in uniq if v[1] in ("printed", "fresh-wallet driven", "automated, concentrated")][:3]
best = [v for v in uniq if v[1] == "organic"][:2]
post = f"Robinhood Chain, {date_h}: {swaps:,} swaps, {100*auto_swaps/max(swaps-sc,1):.0f}% by software, {herds or 0:,} herd minutes, ${printed/1e6:,.0f}M self-cancelling."
if worst: post += " Not real: " + ", ".join(f"{w[0]} ({w[1]})" for w in worst) + "."
if best: post += " Real: " + ", ".join(b[0] for b in best) + "."
post += " Full audit: tetheralabs.com/warning.html"
if len(post) > 280: post = post[:277] + "…"
post_path = os.path.join(ROOT, "site", "warning", f"{a.date}-daily.post.txt")

if a.dry_run:
    print("\n".join(lines)); print("\n--- post ---\n" + post); sys.exit()
subprocess.run([PY, os.path.join(ROOT, "scripts", "publish_issue.py"), issue_path], check=True)
open(post_path, "w").write(post + "\n")
log(f"daily published for {a.date}; post written to {os.path.relpath(post_path, ROOT)} ({len(post)} chars)")
