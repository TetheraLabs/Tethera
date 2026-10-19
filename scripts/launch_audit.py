"""Launch audit: for tokens that launched or ran in a window, say how much of the activity was real.

For each token (ranked by swaps in its pools over the window) the audit reports:
  swaps, wallets, pools, first trade seen
  automated share       swaps and wallets carrying the 7-day behavioural label
  self-cancelling share swaps in the token's pools that cancel inside one transaction (and their USDG if quoted in USDG)
  fresh wallets         wallets whose first swap anywhere in the hot table is in this token's window
  one-swap wallets      wallets whose only swap in the hot table is one swap in this token
  born together         the largest batch of fresh wallets whose first swap landed in the same ten-minute bucket
  top-10 share          share of the token's swaps from its ten busiest wallets
  one-way share         share of wallets that only ever bought, or only ever sold, the token
  nonce <= 2 (--nonce)  share of the token's wallets whose on-chain transaction count is at most 2: funded, one trade, nothing else,
                        whatever date our own tables start at. Fetched from the chain and cached in wallet_nonce.
and a verdict: organic, mixed, automated, fresh-wallet driven, or printed.

Limits: the hot table starts on 10 October 2026, so "first swap" means first since then; wallets active before that look
fresh. Funding-source clustering (wallets funded from one address at one time) needs the first incoming transfer per
wallet and is not in this version.

Usage: python scripts/launch_audit.py [--days 7] [--top 25] [--min-swaps 2000] [--symbols TSI,hi,...] [--out reports/launch_audit.md]
"""
import os, sys, time, argparse
from datetime import datetime, timezone
import psycopg

DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
log = lambda *a: print(time.strftime("%m-%d %H:%M:%S"), *a, flush=True)
ap = argparse.ArgumentParser()
ap.add_argument("--days", type=float, default=7); ap.add_argument("--top", type=int, default=25); ap.add_argument("--min-swaps", type=int, default=2000)
ap.add_argument("--symbols", default="", help="comma-separated symbols to include regardless of rank")
ap.add_argument("--out", default="reports/launch_audit.md")
ap.add_argument("--nonce", action="store_true", help="fetch each wallet's on-chain transaction count (up to --nonce-max wallets per token)")
ap.add_argument("--nonce-max", type=int, default=800)
ap.add_argument("--new-only", action="store_true", help="rank only tokens whose first trade in our data falls inside the window (launches)")
a = ap.parse_args()

CANDIDATES = """
WITH tok AS (
    SELECT t.token, t.symbol, t.name, t.kind, t.official FROM tokens t
    WHERE t.kind NOT IN ('stable', 'eth', 'copycat')
), pl AS (
    SELECT p.pool, tok.token, tok.symbol, p.fee,
           CASE WHEN t0.kind = 'stable' THEN 0 WHEN t1.kind = 'stable' THEN 1 END AS stable_side,
           CASE WHEN t0.kind = 'stable' THEN t0.decimals ELSE t1.decimals END AS dec
    FROM pools p JOIN tok ON tok.token = p.token0 OR tok.token = p.token1
    LEFT JOIN tokens t0 ON t0.token = p.token0 LEFT JOIN tokens t1 ON t1.token = p.token1
), act AS (
    SELECT pl.token, pl.symbol, count(DISTINCT pl.pool) AS pools, sum(m.swaps) AS swaps, min(m.minute) AS first_seen
    FROM pl JOIN pool_minutes m ON m.pool = pl.pool AND m.minute >= %(since)s
    GROUP BY 1, 2
), ever AS (
    -- the token's first trade in all our data (the hot table starts 10 Oct 2026), so "new" means new, not "quiet at the window's start"
    SELECT pl.token, min(m.minute) AS first_ever FROM pl JOIN pool_minutes m ON m.pool = pl.pool WHERE pl.token IN (SELECT token FROM act) GROUP BY 1
)
(SELECT act.token, act.symbol, act.pools, act.swaps, act.first_seen, ever.first_ever FROM act JOIN ever USING (token)
 WHERE act.swaps >= %(min_swaps)s AND (NOT %(new_only)s OR ever.first_ever >= %(since)s) ORDER BY act.swaps DESC LIMIT %(top)s)
UNION
(SELECT token, symbol, pools, swaps, first_seen, first_ever FROM (
    SELECT act.*, ever.first_ever, row_number() OVER (PARTITION BY upper(act.symbol) ORDER BY act.swaps DESC) AS rn FROM act JOIN ever USING (token) WHERE upper(act.symbol) = ANY(%(symbols)s)) x
 WHERE rn <= 2)   -- a launch name gets copied; keep the two busiest contracts per requested symbol
ORDER BY swaps DESC
"""

AUDIT = """
WITH pl AS (
    SELECT p.pool, p.fee,
           CASE WHEN t0.kind = 'stable' THEN 0 WHEN t1.kind = 'stable' THEN 1 END AS stable_side,
           CASE WHEN t0.kind = 'stable' THEN t0.decimals ELSE t1.decimals END AS dec,
           CASE WHEN p.token0 = %(token)s THEN 0 ELSE 1 END AS tok_side
    FROM pools p LEFT JOIN tokens t0 ON t0.token = p.token0 LEFT JOIN tokens t1 ON t1.token = p.token1
    WHERE p.token0 = %(token)s OR p.token1 = %(token)s
), sw AS (
    SELECT s.tx_from AS w, s.block_time, s.is_automated, s.self_cancel, s.direction, pl.tok_side, pl.stable_side, pl.dec, pl.fee,
           CASE WHEN pl.stable_side = 0 THEN abs(s.amount0) / power(10, pl.dec) WHEN pl.stable_side = 1 THEN abs(s.amount1) / power(10, pl.dec) END AS usd,
           -- did this wallet acquire the token (buy) or dispose of it (sell)?
           CASE WHEN (pl.tok_side = 0 AND s.direction = 'buy0') OR (pl.tok_side = 1 AND s.direction = 'sell0') THEN 'buy' ELSE 'sell' END AS side
    FROM swaps s JOIN pl ON pl.pool = s.pool
    WHERE s.block_time >= %(since)s
), tot AS (
    SELECT count(*) AS swaps, count(DISTINCT w) AS wallets, min(block_time) AS first_seen,
           count(*) FILTER (WHERE is_automated AND NOT self_cancel) AS auto_swaps,
           count(*) FILTER (WHERE self_cancel) AS sc_swaps,
           sum(usd) FILTER (WHERE NOT self_cancel) AS organic_usd, sum(usd) FILTER (WHERE self_cancel) AS printed_usd,
           count(DISTINCT w) FILTER (WHERE is_automated) AS auto_wallets
    FROM sw
), perw AS (
    SELECT w, count(*) AS n, min(block_time) AS first_here, bool_or(side = 'buy') AS bought, bool_or(side = 'sell') AS sold FROM sw WHERE NOT self_cancel GROUP BY w
), first_any AS (
    -- the wallet's first swap anywhere in the hot table, and how many swaps it has in total
    SELECT s.tx_from AS w, min(s.block_time) AS first_any, count(*) AS n_any FROM swaps s WHERE s.tx_from IN (SELECT w FROM perw) GROUP BY 1
), joined AS (
    SELECT perw.*, fa.first_any, fa.n_any, (SELECT first_seen FROM tot) AS t0
    FROM perw JOIN first_any fa USING (w)
), fresh AS (
    SELECT w, first_any FROM joined WHERE first_any >= t0
), born AS (
    SELECT date_trunc('hour', first_any) + make_interval(mins => (extract(minute FROM first_any)::int / 10) * 10) AS bucket, count(*) AS n FROM fresh GROUP BY 1
), top10 AS (
    SELECT sum(n) AS n FROM (SELECT n FROM perw ORDER BY n DESC LIMIT 10) x
)
SELECT tot.swaps, tot.wallets, tot.first_seen, tot.auto_swaps, tot.auto_wallets, tot.sc_swaps, tot.organic_usd, tot.printed_usd,
       (SELECT count(*) FROM fresh) AS fresh_wallets,
       (SELECT count(*) FROM joined WHERE n_any = 1) AS one_swap_wallets,
       (SELECT max(n) FROM born) AS born_together, (SELECT bucket FROM born ORDER BY n DESC LIMIT 1) AS born_bucket,
       (SELECT n FROM top10) AS top10_swaps,
       (SELECT count(*) FROM joined WHERE bought AND NOT sold) AS only_bought,
       (SELECT count(*) FROM joined WHERE sold AND NOT bought) AS only_sold,
       (SELECT count(*) FROM joined) AS organic_wallets
FROM tot
"""

def verdict(r):
    swaps, wallets = r["swaps"], r["wallets"]
    if not swaps: return "no activity"
    new_token = r.get("new_token", False)
    sc = r["sc_swaps"] / swaps
    printed_share = (r["printed_usd"] or 0) / ((r["organic_usd"] or 0) + (r["printed_usd"] or 0)) if (r["organic_usd"] or r["printed_usd"]) else sc
    auto = r["auto_swaps"] / max(swaps - r["sc_swaps"], 1)
    ow = r["organic_wallets"] or 1
    fresh = r["fresh_wallets"] / ow; one = r["one_swap_wallets"] / ow; top10 = r["top10_swaps"] / max(swaps - r["sc_swaps"], 1)
    born = (r["born_together"] or 0) / ow
    if printed_share >= 0.5: return "printed"
    if auto >= 0.6 and top10 >= 0.5: return "automated, concentrated"
    if auto >= 0.6: return "automated"
    if new_token and fresh >= 0.8: return "fresh-wallet driven"
    if fresh >= 0.6 and (one >= 0.4 or born >= 0.25): return "fresh-wallet driven"
    if auto < 0.35 and top10 < 0.3 and (fresh < 0.6 or not new_token): return "organic"
    return "mixed"

t0 = time.time()
since = datetime.now(timezone.utc) - __import__("datetime").timedelta(days=a.days)
symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
def nonce_shares(conn, token, since, limit):
    """Share of the token's (organic) wallets with an on-chain transaction count <= 2 and <= 5, fetching what is not cached."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from tethera.rpc import live_rpc
    seen = dict(conn.execute("""
        SELECT s.tx_from, count(*) FROM swaps s JOIN pools p ON p.pool = s.pool
        WHERE (p.token0 = %s OR p.token1 = %s) AND s.block_time >= %s AND NOT s.self_cancel
        GROUP BY 1 ORDER BY 2 DESC LIMIT %s""", (token, token, since, limit)).fetchall())
    wallets = list(seen)
    if not wallets: return None
    have = dict(conn.execute("SELECT wallet, nonce FROM wallet_nonce WHERE wallet = ANY(%s) AND fetched_at > now() - interval '7 days'", (wallets,)).fetchall())
    todo = [w for w in wallets if w not in have]
    if todo:
        rpc = live_rpc()
        for i in range(0, len(todo), 20):
            group = todo[i:i + 20]
            try:
                res = rpc.batch([("eth_getTransactionCount", [w, "latest"]) for w in group], lenient=True)
            except Exception as e:
                log(f"  nonce batch failed: {str(e)[:60]}"); time.sleep(3); continue
            for w, v in zip(group, res):
                if v is not None:
                    have[w] = int(v, 16)
                    conn.execute("INSERT INTO wallet_nonce (wallet, nonce) VALUES (%s, %s) ON CONFLICT (wallet) DO UPDATE SET nonce = EXCLUDED.nonce, fetched_at = now()", (w, have[w]))
            conn.commit()
            time.sleep(0.6)   # leave the free endpoints to the follower; the audit is not in a hurry
    vals = [(have[w], seen[w]) for w in wallets if w in have]
    if not vals: return None
    # "born for this": the wallet's whole transaction count fits inside the swaps we saw it make in this token, plus two
    born_for_this = sum(1 for n, k in vals if n <= k + 2) / len(vals)
    return {"n": len(vals), "le2": sum(1 for n, _ in vals if n <= 2) / len(vals), "born_for_this": born_for_this, "median": sorted(n for n, _ in vals)[len(vals) // 2]}

rows = []
with psycopg.connect(DB) as conn:
    conn.execute("SET work_mem = '512MB'"); conn.execute("SET statement_timeout = '30min'")
    cands = conn.execute(CANDIDATES, {"since": since, "min_swaps": a.min_swaps, "symbols": symbols, "top": a.top, "new_only": a.new_only}).fetchall()
    log(f"{len(cands)} candidate tokens ({time.time()-t0:.0f}s)")
    for token, symbol, pools, swaps, first_seen, first_ever in cands:
        t = time.time()
        cols = ["swaps", "wallets", "first_seen", "auto_swaps", "auto_wallets", "sc_swaps", "organic_usd", "printed_usd", "fresh_wallets",
                "one_swap_wallets", "born_together", "born_bucket", "top10_swaps", "only_bought", "only_sold", "organic_wallets"]
        r = dict(zip(cols, conn.execute(AUDIT, {"token": token, "since": since}).fetchone()))
        r.update(token=token, symbol=symbol, pools=pools, new_token=(first_ever >= since))
        r["nonce"] = nonce_shares(conn, token, since, a.nonce_max) if a.nonce else None
        if r["nonce"] and r["new_token"] and r["nonce"]["born_for_this"] >= 0.5: r["one_trade_wallets"] = True
        r["verdict"] = verdict(r)
        if r.get("one_trade_wallets") and r["verdict"] in ("mixed", "organic"): r["verdict"] = "fresh-wallet driven"
        rows.append(r); log(f"{symbol:12} {r['swaps']:>8,} swaps {r['wallets']:>7,} wallets -> {r['verdict']} ({time.time()-t:.0f}s)")

def pct(x, d): return f"{100*x/d:.0f}%" if d else "–"
def usd(x): return f"${x/1e6:,.1f}M" if x and x >= 1e6 else (f"${x:,.0f}" if x else "–")
L = [f"# Launch audit, {a.days:g} days to {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", "",
     "Tokens ranked by swaps in their pools over the window. Shares are of organic (non-self-cancelling) swaps or wallets. "
     "\"Fresh\" = first swap anywhere in our hot table (which starts 10 Oct 2026) was in this token's window; \"one swap\" = the wallet's only swap in the hot table; "
     "\"born together\" = the largest batch of fresh wallets whose first swap fell in one ten-minute bucket.", "",
     "| token | first seen (UTC) | pools | swaps | wallets | automated swaps | automated wallets | self-cancelling | printed $ | organic $ | fresh wallets | one-swap wallets | born together | top-10 share | only bought / only sold | born for this / nonce ≤ 2 (median nonce) | verdict |",
     "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
for r in rows:
    org = r["swaps"] - r["sc_swaps"]; ow = r["organic_wallets"] or 0
    L.append(f"| {r['symbol']} | {r['first_seen']:%d %b %H:%M}{'' if r['new_token'] else ' (before window)'} | {r['pools']} | {r['swaps']:,} | {r['wallets']:,} | {pct(r['auto_swaps'], org)} | {pct(r['auto_wallets'], r['wallets'])} | "
             f"{pct(r['sc_swaps'], r['swaps'])} | {usd(r['printed_usd'])} | {usd(r['organic_usd'])} | {pct(r['fresh_wallets'], ow) if r['new_token'] else 'n/a'} | {pct(r['one_swap_wallets'], ow)} | "
             f"{(str(r['born_together'] or 0) + ' (' + (r['born_bucket'] or r['first_seen']).strftime('%d %b %H:%M') + ')') if r['new_token'] else 'n/a'} | {pct(r['top10_swaps'], org)} | {pct(r['only_bought'], ow)} / {pct(r['only_sold'], ow)} | {(f"{100*r['nonce']['born_for_this']:.0f}% / {100*r['nonce']['le2']:.0f}% ({r['nonce']['median']})" if r.get('nonce') else '–')} | **{r['verdict']}** |")
L += ["", "## Reading the columns", "",
      "- **automated**: the 7-day behavioural label; arbitrage and market-making bots count, so an established pool reads 60%+ and that is normal. On a day-old token it is a sign that bots arrived, not that people did.",
      "- **self-cancelling / printed**: swaps that go both ways in one transaction and settle nothing; the dollar figures need a USDG-quoted pool.",
      "- **fresh / one-swap / born together**: a launch bought by thousands of wallets that have never traded before, trade once, and appeared in the same ten minutes is a launch bought by one operator with a script. A launch bought by wallets with history is a launch bought by people.",
      "- **top-10 share**: ten wallets doing half the swaps is a concentrated market whatever the wallet count says.",
      "- **only bought / only sold**: a crowd that only ever bought is holders or bags; a crowd that only ever sold is an airdrop being dumped.",
      "- **born for this**: the wallet's on-chain transaction count is no more than the swaps we saw it make in this token plus two (a funding transfer and an approval). Its whole life is this token. **nonce ≤ 2**: funded, one trade, nothing else. Neither depends on where our own data starts.",
      "", "Not in this version: wallets funded from the same source at the same time (needs each wallet's first incoming transfer)."]
os.makedirs(os.path.dirname(a.out), exist_ok=True); open(a.out, "w").write("\n".join(L) + "\n")
log(f"wrote {a.out} ({len(rows)} tokens, {time.time()-t0:.0f}s)")
