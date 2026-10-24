"""One-minute bars for each instrument from the chain's own fills: every swap in every pool that pairs the
official stock token with USDG, hot table and archive together. Price is USDG per share. Self-cancelling
swaps are dropped; within a minute, prints more than 15% away from that minute's median are dropped as
bad fills before open/high/low/close are taken. Stored in desk_bars_1m and advanced by watermark.

  python -m tethera.chain_bars --build              # from 1 July 2026 (or the watermark) to now, all instruments
  python -m tethera.chain_bars --build --symbol SPY --from 2026-10-15 --to 2026-10-16   # a slice (for tests)
  python -m tethera.chain_bars --build --hot-only   # only the live table (last ~10 days); the 5-minute updater uses this
Anyone with the swap tables can rebuild the same bars: the rule is the SQL below and nothing else.
Incremental builds restart at the watermark minute, which is deleted first so a partly filled minute is rebuilt whole.
"""
import os, sys, time
from datetime import datetime, timedelta, timezone
import psycopg
from .instruments import TOKENS, USDG, CHAIN_START

DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
def arg(n, d): return sys.argv[sys.argv.index(n) + 1] if n in sys.argv else d
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)

DDL = """
CREATE TABLE IF NOT EXISTS desk_bars_1m (
    symbol      TEXT NOT NULL,
    minute      TIMESTAMPTZ NOT NULL,
    open        DOUBLE PRECISION NOT NULL, high DOUBLE PRECISION NOT NULL, low DOUBLE PRECISION NOT NULL, close DOUBLE PRECISION NOT NULL,
    vwap        DOUBLE PRECISION NOT NULL,
    volume_usd  DOUBLE PRECISION NOT NULL,
    swaps       INTEGER NOT NULL,
    auto_swaps  INTEGER NOT NULL,
    pools       INTEGER NOT NULL,
    dropped     INTEGER NOT NULL,
    PRIMARY KEY (symbol, minute)
)"""

BARS = """
WITH raw AS (SELECT sw.*, p.tok_side FROM ({source}) sw JOIN (VALUES {pools}) AS p(pool, tok_side) ON p.pool = sw.pool),
sc AS (SELECT date_trunc('minute', block_time) AS minute, count(*) AS n FROM raw WHERE self_cancel GROUP BY 1),
s AS (
    SELECT date_trunc('minute', block_time) AS minute, block_number, log_index, pool, coalesce(is_automated, false) AS auto,
           CASE WHEN tok_side = 0 THEN abs(amount1) / 1e6 ELSE abs(amount0) / 1e6 END AS usd,
           CASE WHEN tok_side = 0 THEN (abs(amount1) / 1e6) / nullif(abs(amount0) / 1e18, 0)
                ELSE (abs(amount0) / 1e6) / nullif(abs(amount1) / 1e18, 0) END AS px
    FROM raw x WHERE NOT self_cancel
), med AS (SELECT minute, percentile_cont(0.5) WITHIN GROUP (ORDER BY px) AS m FROM s WHERE px > 0 GROUP BY 1),
ok AS (SELECT s.* FROM s JOIN med USING (minute) WHERE px > 0 AND px BETWEEN 0.85 * m AND 1.15 * m AND usd > 0),
dropped AS (SELECT minute, count(*) AS n FROM s LEFT JOIN ok USING (minute, block_number, log_index) WHERE ok.px IS NULL GROUP BY 1)
SELECT ok.minute,
       (array_agg(px ORDER BY block_number, log_index))[1] AS open, max(px) AS high, min(px) AS low,
       (array_agg(px ORDER BY block_number DESC, log_index DESC))[1] AS close,
       sum(usd) / sum(usd / px) AS vwap, sum(usd) AS volume_usd, count(*) AS swaps, count(*) FILTER (WHERE auto) AS auto_swaps,   -- share-weighted: total dollars over total shares
       count(DISTINCT pool) AS pools, coalesce(max(d.n), 0) AS dropped, coalesce(max(sc.n), 0) AS sc_swaps
FROM ok LEFT JOIN dropped d USING (minute) LEFT JOIN sc USING (minute) GROUP BY ok.minute
"""

def pools_for(conn, token):
    rows = conn.execute("SELECT pool, CASE WHEN token0 = %s THEN 0 ELSE 1 END FROM pools WHERE (token0 = %s AND token1 = %s) OR (token1 = %s AND token0 = %s)",
                        (token, token, USDG, token, USDG)).fetchall()
    return rows

SRC = """SELECT pool, block_number, log_index, block_time, amount0, amount1, is_automated, self_cancel FROM {t}
         WHERE block_time >= %(a)s AND block_time < %(b)s AND pool = ANY(%(pools)s)"""

def build(conn, symbol, a, b, tables=("swaps_archive", "swaps")):
    """One pass over both tables, UNION-deduplicated on (tx hash, log index) via the full row, so a minute that straddles
    the archive boundary is aggregated whole and a swap present in both tables counts once."""
    token = TOKENS[symbol]; pools = pools_for(conn, token)
    if not pools:
        log(symbol, "no USDG pools"); return 0
    values = ",".join(f"('{p}',{s})" for p, s in pools); pool_ids = [p for p, _ in pools]
    source = " UNION ".join(SRC.format(t=t) for t in tables)          # UNION (not ALL) removes any row present in both tables
    rows = conn.execute(BARS.format(source=source, pools=values), {"a": a, "b": b, "pools": pool_ids}).fetchall()
    if rows:
        with conn.cursor() as cur:
            cur.executemany("""INSERT INTO desk_bars_1m (symbol, minute, open, high, low, close, vwap, volume_usd, swaps, auto_swaps, pools, dropped, sc_swaps)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (symbol, minute) DO UPDATE SET open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close,
                                 vwap=EXCLUDED.vwap, volume_usd=EXCLUDED.volume_usd, swaps=EXCLUDED.swaps, auto_swaps=EXCLUDED.auto_swaps, pools=EXCLUDED.pools, dropped=EXCLUDED.dropped, sc_swaps=EXCLUDED.sc_swaps""",
                            [(symbol, *r) for r in rows])
        conn.commit()
    return len(rows)

def main():
    symbols = [arg("--symbol", None)] if "--symbol" in sys.argv else list(TOKENS)
    with psycopg.connect(DB) as conn:
        conn.execute("SET statement_timeout = 0"); conn.execute("SET work_mem = '256MB'"); conn.execute(DDL); conn.commit()
        for sym in symbols:
            if "--from" in sys.argv:
                a = datetime.fromisoformat(arg("--from", CHAIN_START)).replace(tzinfo=timezone.utc)
            else:
                (wm,) = conn.execute("SELECT max(minute) FROM desk_bars_1m WHERE symbol = %s", (sym,)).fetchone()
                if wm:
                    conn.execute("DELETE FROM desk_bars_1m WHERE symbol = %s AND minute >= %s", (sym, wm)); conn.commit(); a = wm
                else:
                    a = datetime.fromisoformat(CHAIN_START).replace(tzinfo=timezone.utc)
                if "--hot-only" in sys.argv:
                    (hmin,) = conn.execute("SELECT min(block_time) FROM swaps WHERE block_time > now() - interval '30 days'").fetchone()
                    if hmin is None: log(sym, "hot table is empty"); continue
                    a = max(a, hmin.replace(second=0, microsecond=0))
            b_end = datetime.fromisoformat(arg("--to", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M"))).replace(tzinfo=timezone.utc)
            t0 = time.time(); total = 0
            while a < b_end:
                b = min(a + timedelta(days=1), b_end)
                total += build(conn, sym, a, b, tables=("swaps",) if "--hot-only" in sys.argv else ("swaps_archive", "swaps")); a = b
            log(f"{sym}: {total:,} bars to {b_end:%Y-%m-%d %H:%M} in {time.time()-t0:,.0f}s")

if __name__ == "__main__":
    main()
