"""Shepherd's Warning: the weekly herding report, generated from pool_minutes and swaps.

Usage: python scripts/shepherds_warning.py [--to 2026-10-17] [--days 7] [--out reports/shepherds_warning_YYYY-MM-DD.md]
Reads the hot tables only (bounded by time). Produces a markdown draft; the published issue is edited by hand.
"""
import os, sys, time
from datetime import datetime, timezone, timedelta
import psycopg

def arg(name, default): return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default
DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
TO = datetime.fromisoformat(arg("--to", datetime.now(timezone.utc).strftime("%Y-%m-%d"))).replace(tzinfo=timezone.utc)
DAYS = int(arg("--days", "7")); FROM = TO - timedelta(days=DAYS)
OUT = arg("--out", os.path.join(os.path.dirname(__file__), "..", "reports", f"shepherds_warning_{TO:%Y-%m-%d}.md"))
HERD = 15   # automated wallets one way inside a minute, with the other side <= 15% of it
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)

def label(cur, pool):
    cur.execute("""SELECT coalesce(a.symbol, left(p.token0, 8)), coalesce(b.symbol, left(p.token1, 8)), p.protocol, a.kind, b.kind
                   FROM pools p LEFT JOIN tokens a ON a.token = p.token0 LEFT JOIN tokens b ON b.token = p.token1 WHERE p.pool = %s""", (pool,))
    r = cur.fetchone()
    if not r: return f"{pool[:10]}…", None
    kind = "stock" if "stock" in (r[3], r[4]) else None
    return f"{r[0]}/{r[1]} ({r[2]})", kind

def main():
    with psycopg.connect(DB) as conn, conn.cursor() as cur:
        cur.execute("SET work_mem = '256MB'")
        cur.execute("""SELECT count(*), count(DISTINCT tx_from), count(*) FILTER (WHERE is_automated),
                              count(DISTINCT tx_from) FILTER (WHERE is_automated), count(DISTINCT pool)
                       FROM swaps WHERE block_time >= %s AND block_time < %s""", (FROM, TO))
        n_swaps, n_wallets, n_auto, n_auto_w, n_pools = cur.fetchone()
        cur.execute("SELECT count(*), coalesce(sum(n_swaps),0) FROM wallet_features WHERE is_volume_bot"); vb, vb_swaps = cur.fetchone()
        # herd minutes: one-sided automated crowds
        cur.execute("""
            WITH m AS (
              SELECT pool, minute, direction, auto_wallets AS same,
                     coalesce((SELECT o.auto_wallets FROM pool_minutes o WHERE o.pool = pm.pool AND o.minute = pm.minute AND o.direction <> pm.direction), 0) AS opp
              FROM pool_minutes pm WHERE minute >= %s AND minute < %s AND auto_wallets >= %s)
            SELECT pool, minute, direction, same, opp FROM m WHERE opp <= 0.15 * same ORDER BY same DESC""", (FROM, TO, HERD))
        herds = cur.fetchall()
        # episodes: consecutive herd minutes in the same pool and direction
        episodes = {}
        for pool, minute, d, same, opp in sorted(herds, key=lambda r: (r[0], r[2], r[1])):
            key = (pool, d); ep = episodes.setdefault(key, [])
            if ep and (minute - ep[-1]["end"]) <= timedelta(minutes=1):
                ep[-1]["end"] = minute; ep[-1]["minutes"] += 1; ep[-1]["peak"] = max(ep[-1]["peak"], same)
            else:
                ep.append({"start": minute, "end": minute, "minutes": 1, "peak": same})
        all_eps = [dict(pool=k[0], direction=k[1], **e) for k, v in episodes.items() for e in v]
        n_eps = len(all_eps); long_eps = sum(1 for e in all_eps if e["minutes"] > 1)
        by_pool = {}
        for e in all_eps: by_pool.setdefault(e["pool"], []).append(e)
        # busiest minute on the chain
        cur.execute("""SELECT minute, sum(auto_wallets), count(DISTINCT pool) FROM pool_minutes WHERE minute >= %s AND minute < %s
                       GROUP BY minute ORDER BY 2 DESC LIMIT 1""", (FROM, TO))
        busiest = cur.fetchone()
        # calm pools: busy pools with no herd minute
        cur.execute("""SELECT pool, sum(swaps) FROM pool_minutes WHERE minute >= %s AND minute < %s GROUP BY pool ORDER BY 2 DESC LIMIT 30""", (FROM, TO))
        busy = cur.fetchall(); calm = [(p, n) for p, n in busy if p not in by_pool][:5]
        # cost: entry impact in herd minutes vs solo minutes, same pools (basis points, median)
        cur.execute("""
            WITH px AS (
              SELECT pool, minute, direction, auto_wallets, px_median,
                     lag(px_median) OVER (PARTITION BY pool ORDER BY minute, direction) AS px_prev
              FROM pool_minutes WHERE minute >= %s AND minute < %s AND px_median IS NOT NULL AND px_median > 0)
            SELECT grp, count(*),
                   1e4 * percentile_cont(0.5) WITHIN GROUP (ORDER BY (CASE WHEN direction='buy0' THEN 1 ELSE -1 END) * (px_median / px_prev - 1))
            FROM (SELECT *, CASE WHEN auto_wallets >= %s THEN 'herd' WHEN auto_wallets BETWEEN 1 AND 4 THEN 'solo' END AS grp FROM px) g
            WHERE px_prev > 0 AND grp IS NOT NULL GROUP BY 1""",
                    (FROM, TO, HERD))
        cost = {g: (n, bp) for g, n, bp in cur.fetchall()}
        top = sorted(all_eps, key=lambda e: -e["peak"])[:10]
        L = [f"# Shepherd's Warning — week to {TO:%-d %B %Y}\n",
             f"*Where the herd ran on Robinhood Chain, {FROM:%-d %B} to {TO:%-d %B %Y}. Measured from every Uniswap swap on the chain; automated wallets identified by behaviour, not by self-declaration. Free, weekly, from Tethera Labs.*\n",
             "## The week in numbers\n",
             f"- {n_swaps:,} swaps in {n_pools:,} pools by {n_wallets:,} wallets.",
             f"- {n_auto / max(n_swaps,1):.0%} of swaps came from {n_auto_w:,} wallets we label automated. {vb:,} of those run paid volume services and are left out of the herd counts below.",
             f"- {n_eps:,} herds: minutes where {HERD}+ automated wallets hit one pool from the same side with almost nobody on the other. {long_eps:,} of them lasted longer than a minute.",
             (f"- The most crowded minute was {busiest[0]:%a %-d %b %H:%M} UTC: {int(busiest[1]):,} automated wallet-trades across {busiest[2]:,} pools." if busiest else ""),
             ]
        if "herd" in cost and "solo" in cost:
            L.append(f"- Price at entry: inside a herd, the median automated trade paid {cost['herd'][1]:+.1f} bp against the last price; trading alone, {cost['solo'][1]:+.1f} bp. ({cost['herd'][0]:,} herd minutes, {cost['solo'][0]:,} solo minutes.)")
        L.append("\n## Where the herd ran\n")
        L.append("| pool | side | started (UTC) | lasted | peak wallets in a minute |\n|---|---|---|---|---|")
        for e in top:
            name, kind = label(cur, e["pool"])
            L.append(f"| {name}{' · stock' if kind else ''} | {'buying' if e['direction']=='buy0' else 'selling'} token0 | {e['start']:%a %-d %b %H:%M} | {e['minutes']} min | {e['peak']} |")
        L.append("\n## Pools that herded most often\n")
        for pool, eps in sorted(by_pool.items(), key=lambda kv: -len(kv[1]))[:8]:
            name, kind = label(cur, pool)
            L.append(f"- {name}{' · stock' if kind else ''}: {len(eps)} herds, longest {max(e['minutes'] for e in eps)} min, biggest {max(e['peak'] for e in eps)} wallets")
        L.append("\n## Busy but calm\n")
        for pool, n in calm:
            name, kind = label(cur, pool); L.append(f"- {name}: {int(n):,} swaps, no herd minute all week")
        L.append("\n## How to read this\n")
        L.append(f"A herd is {HERD} or more automated wallets trading the same pool the same way inside one minute, with the other side at most 15% of that. "
                 "Automated means the wallet trips at least three of seven behavioural tests (sub-second gaps, same-block multi-pool trades, metronomic timing, fast round trips, computed sizes, reacting to others within two blocks, persistence). "
                 "Volume bots are automated wallets that sit in one pool with no net position and a fixed daily budget; they are counted as automated but not as herd. "
                 "Entry cost is the trade's median price against the last price in the pool before that minute, in basis points, signed so that positive means paid more.\n")
        L.append("*Tethera Labs · github.com/TetheraLabs · TetheraLabs@proton.me*\n")
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        open(OUT, "w").write("\n".join(L))
        log(f"wrote {OUT}: {n_eps} herds, {len(by_pool)} pools")

if __name__ == "__main__":
    main()
