"""Same-transaction round trips: swaps in one transaction, one pool, both directions, with the stablecoin side netting to
(almost) nothing. Under Uniswap v4 flash accounting such a trip settles nothing, so it is free in a zero-fee pool and prints
whatever volume its author likes. This counts the gross dollars in such trips, week by week and by fee tier, against all
priced (USDG-quoted) volume, so the claim "x% of volume is printed" can be checked over months rather than one week.

Usage: python scripts/wash_check.py [--table swaps_archive] [--from 2026-07-20] [--to 2026-10-18] [--net 0.01] [--out reports/wash_check.md]
"""
import os, sys, time, argparse
from datetime import datetime, timedelta, timezone
import psycopg

DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)
ap = argparse.ArgumentParser()
ap.add_argument("--table", default="swaps_archive"); ap.add_argument("--from", dest="from_", required=True); ap.add_argument("--to", required=True)
ap.add_argument("--net", type=float, default=0.01, help="a trip is a round trip when |net stable| < this share of gross")
ap.add_argument("--out", default="reports/wash_check.md")
a = ap.parse_args()
frm = datetime.fromisoformat(a.from_).replace(tzinfo=timezone.utc); to = datetime.fromisoformat(a.to).replace(tzinfo=timezone.utc)

SQL = f"""
WITH sq AS (
    SELECT s.tx_hash, s.pool, s.direction, s.tx_from, p.fee,
           CASE WHEN t0.kind = 'stable' THEN abs(s.amount0) / power(10, t0.decimals) ELSE abs(s.amount1) / power(10, t1.decimals) END AS usd,
           CASE WHEN t0.kind = 'stable' THEN s.amount0 / power(10, t0.decimals) ELSE s.amount1 / power(10, t1.decimals) END AS net
    FROM {a.table} s
    JOIN pools p ON p.pool = s.pool
    LEFT JOIN tokens t0 ON t0.token = p.token0
    LEFT JOIN tokens t1 ON t1.token = p.token1
    WHERE s.block_time >= %(frm)s AND s.block_time < %(to)s
      AND ((t0.kind = 'stable' AND t0.symbol = 'USDG' AND coalesce(t1.kind, 'other') NOT IN ('stable', 'copycat'))
        OR (t1.kind = 'stable' AND t1.symbol = 'USDG' AND coalesce(t0.kind, 'other') NOT IN ('stable', 'copycat')))
), tp AS (
    SELECT tx_hash, pool, min(fee) AS fee, min(tx_from) AS tx_from, count(*) AS n, count(DISTINCT direction) AS d,
           sum(usd) AS usd, abs(sum(net)) AS netusd
    FROM sq GROUP BY 1, 2
)
SELECT CASE WHEN fee = 0 THEN 'zero-fee' WHEN fee = 8388608 THEN 'dynamic' ELSE 'other' END AS tier,
       count(DISTINCT pool) AS pools, count(*) AS txpools, sum(n) AS swaps, round(sum(usd)) AS usd,
       count(*) FILTER (WHERE d = 2 AND netusd < %(net)s * usd) AS rt_txpools,
       count(DISTINCT pool) FILTER (WHERE d = 2 AND netusd < %(net)s * usd) AS rt_pools,
       count(DISTINCT tx_from) FILTER (WHERE d = 2 AND netusd < %(net)s * usd) AS rt_senders,
       round(coalesce(sum(usd) FILTER (WHERE d = 2 AND netusd < %(net)s * usd), 0)) AS rt_usd
FROM tp GROUP BY 1 ORDER BY 1
"""
rows = []
with psycopg.connect(DB) as conn:
    conn.execute("SET work_mem = '512MB'"); conn.execute("SET statement_timeout = '2h'")
    wk = frm
    while wk < to:
        nxt = min(wk + timedelta(days=7), to); t = time.time()
        res = conn.execute(SQL, {"frm": wk, "to": nxt, "net": a.net}).fetchall()
        for r in res: rows.append((wk.date(),) + tuple(r))
        tot = sum(r[4] for r in res); rt = sum(r[8] for r in res)
        log(f"week {wk:%Y-%m-%d}: ${tot:,.0f} priced, ${rt:,.0f} round trips ({rt/tot if tot else 0:.1%}) ({time.time()-t:.0f}s)")
        wk = nxt

os.makedirs(os.path.dirname(a.out), exist_ok=True)
L = [f"# Same-transaction round trips, {frm:%Y-%m-%d} to {to:%Y-%m-%d} (`{a.table}`)", "",
     f"A round trip is one transaction swapping both ways in one USDG-quoted pool with the USDG side netting to under {a.net:.0%} of the gross. "
     "Copycat stablecoins and copycat-quoted pools are excluded. Volume is the USDG side.", "",
     "| week | tier | pools | tx-pools | swaps | volume | round-trip volume | share | rt pools | rt senders |", "|---|---|---|---|---|---|---|---|---|---|"]
for r in rows:
    L.append(f"| {r[0]} | {r[1]} | {r[2]:,} | {r[3]:,} | {r[4]:,} | ${r[5]:,.0f} | ${r[9]:,.0f} | {r[9]/r[5] if r[5] else 0:.1%} | {r[7]:,} | {r[8]:,} |")
tot = sum(r[5] for r in rows); rt = sum(r[9] for r in rows)
by = {}
for r in rows: by.setdefault(r[1], [0, 0]); by[r[1]][0] += r[5]; by[r[1]][1] += r[9]
L += ["", "## Whole period", "", "| tier | volume | round-trip volume | share |", "|---|---|---|---|"]
for k, (v, w) in sorted(by.items()): L.append(f"| {k} | ${v:,.0f} | ${w:,.0f} | {w/v if v else 0:.1%} |")
L += [f"| **all** | ${tot:,.0f} | ${rt:,.0f} | {rt/tot if tot else 0:.1%} |"]
open(a.out, "w").write("\n".join(L) + "\n"); log(f"wrote {a.out}: ${tot:,.0f} priced, ${rt:,.0f} round trips ({rt/tot if tot else 0:.1%})")
