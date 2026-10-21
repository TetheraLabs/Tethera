"""The real list: every token on the chain ranked by organic activity, rebuilt hourly in the API process.

For each token (stock tokens and everything else; stables, ETH and copycats are not ranked) over the last 24 hours and
the last 7 days: organic swaps, distinct wallets, automated share, self-cancelling (printed) share and USD where a pool is
quoted in a stablecoin, whether the token first traded inside the window, and the cached 7-day audit verdict when one
is under a day old. Anonymous callers see the top FREE_ROWS; a key opens the whole list and the filters.
"""
import threading, time
from datetime import datetime, timezone, timedelta

from . import db
from .audit import audit_token

FREE_ROWS, MAX_ROWS, REBUILD_S, PREAUDIT = 10, 200, 3600, 20
_lists: dict[str, dict] = {}
_lock = threading.Lock()
_started = False

BY_POOL = """
WITH pl AS (
    SELECT p.pool, p.token0, p.token1, t0.kind AS k0, t1.kind AS k1,
           CASE WHEN t0.kind = 'stable' THEN 0 WHEN t1.kind = 'stable' THEN 1 END AS stable_side,
           CASE WHEN t0.kind = 'stable' THEN t0.decimals ELSE t1.decimals END AS dec,
           CASE WHEN t0.kind = 'eth' THEN 0 WHEN t1.kind = 'eth' THEN 1 END AS eth_side
    FROM pools p LEFT JOIN tokens t0 ON t0.token = p.token0 LEFT JOIN tokens t1 ON t1.token = p.token1
), m AS (
    SELECT pm.pool, sum(pm.swaps) AS swaps, sum(pm.auto_swaps) AS auto_swaps, min(pm.minute) AS first_m,
           sum(CASE WHEN pl.stable_side = 0 THEN pm.amount0_in / power(10, pl.dec) WHEN pl.stable_side = 1 THEN pm.amount1_in / power(10, pl.dec) END) AS usd,
           sum(CASE WHEN pl.stable_side IS NULL AND pl.eth_side = 0 THEN pm.amount0_in / 1e18 WHEN pl.stable_side IS NULL AND pl.eth_side = 1 THEN pm.amount1_in / 1e18 END) AS eth
    FROM pool_minutes pm JOIN pl ON pl.pool = pm.pool WHERE pm.minute >= %(since)s GROUP BY 1
), sc AS (
    SELECT s.pool, count(*) AS sc,
           sum(CASE WHEN pl.stable_side = 0 THEN abs(s.amount0) / power(10, pl.dec) WHEN pl.stable_side = 1 THEN abs(s.amount1) / power(10, pl.dec) END) AS printed_usd
    FROM swaps s JOIN pl ON pl.pool = s.pool WHERE s.self_cancel AND s.block_time >= %(since)s GROUP BY 1
)
SELECT pl.pool, pl.token0, pl.token1, pl.k0, pl.k1, m.swaps, m.auto_swaps, m.first_m, m.usd, coalesce(sc.sc, 0), sc.printed_usd, m.eth
FROM m JOIN pl ON pl.pool = m.pool LEFT JOIN sc ON sc.pool = m.pool WHERE m.swaps > 0
"""
WALLETS = "SELECT pool, count(DISTINCT tx_from) FROM swaps WHERE pool = ANY(%(pools)s) AND block_time >= %(since)s AND NOT self_cancel GROUP BY 1"
FIRST_EVER = "SELECT pool, min(minute) FROM pool_minutes WHERE pool = ANY(%(pools)s) GROUP BY 1"
META = """SELECT lower(t.token), t.symbol, t.name, t.kind, t.official, a.result ->> 'verdict', a.computed_at
          FROM tokens t LEFT JOIN token_audits a ON a.token = lower(t.token) AND a.days = 7 AND a.computed_at > now() - interval '24 hours'
          WHERE lower(t.token) = ANY(%(toks)s)"""
RANKED = ("stock", "other", None)
ETH_PX = """
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY CASE WHEN t0.kind = 'eth' THEN m.px_median * power(10, t0.decimals - t1.decimals)
                                                         ELSE 1 / nullif(m.px_median * power(10, t0.decimals - t1.decimals), 0) END)
FROM pool_minutes m JOIN pools p ON p.pool = m.pool JOIN tokens t0 ON t0.token = p.token0 JOIN tokens t1 ON t1.token = p.token1
WHERE m.minute > now() - interval '6 hours' AND m.px_median > 0 AND m.swaps >= 3
  AND ((t0.kind = 'eth' AND t1.kind = 'stable') OR (t0.kind = 'stable' AND t1.kind = 'eth'))"""


def eth_price(conn) -> float | None:
    """Median ETH price in stablecoin terms over the last six hours of ETH/stable pool minutes; None when it looks wrong."""
    px = conn.execute(ETH_PX).fetchone()[0]
    return float(px) if px and 200 < float(px) < 50_000 else None


def build(hours: int) -> dict:
    """Per pool from the minute aggregates (cheap), printed volume from the self-cancel index, then per token; distinct
    wallets and first-ever only for the pools of the ranked tokens."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    with db.connect() as conn:
        conn.execute("SET work_mem = '256MB'"); conn.execute("SET statement_timeout = '5min'")
        pools = conn.execute(BY_POOL, {"since": since}).fetchall()
        eth_usd = eth_price(conn)
        by_tok: dict[str, dict] = {}
        for pool, t0, t1, k0, k1, swaps, auto, first_m, usd, sc, printed, eth in pools:
            if usd is None and eth is not None and eth_usd: usd = float(eth) * eth_usd    # ETH-quoted pool, priced at the chain's own ETH rate
            for tok, kind in ((t0, k0), (t1, k1)):
                if kind not in RANKED: continue
                d = by_tok.setdefault(tok.lower(), {"token": tok, "swaps": 0, "auto": 0, "sc": 0, "usd": 0.0, "usd_known": False, "printed": 0.0, "pools": [], "first_m": first_m})
                d["swaps"] += int(swaps); d["auto"] += int(auto); d["sc"] += int(sc); d["pools"].append(pool)
                if usd is not None: d["usd"] += float(usd); d["usd_known"] = True
                if printed is not None: d["printed"] += float(printed)
                if first_m < d["first_m"]: d["first_m"] = first_m
        top = sorted((d for d in by_tok.values() if d["swaps"] >= 20), key=lambda d: -d["swaps"])[:MAX_ROWS]
        pool_ids = [p for d in top for p in d["pools"]]
        wallets = dict(conn.execute(WALLETS, {"pools": pool_ids, "since": since}).fetchall()) if pool_ids else {}
        first_ever = dict(conn.execute(FIRST_EVER, {"pools": pool_ids}).fetchall()) if pool_ids else {}
        meta = {r[0]: r for r in conn.execute(META, {"toks": [d["token"].lower() for d in top]}).fetchall()}
    utc = lambda t: t.astimezone(timezone.utc).isoformat(timespec="minutes") if t else None
    now = datetime.now(timezone.utc); out = []
    for i, d in enumerate(top, 1):
        mt = meta.get(d["token"].lower()); gross = d["usd"] + d["printed"]
        fe = min((first_ever[p] for p in d["pools"] if p in first_ever), default=None)
        w = max((wallets.get(p, 0) for p in d["pools"]), default=0)
        out.append({"rank": i, "token": d["token"], "symbol": mt[1] if mt else None, "name": mt[2] if mt else None, "kind": (mt[3] if mt else None) or "unknown",
                    "official_stock_token": bool(mt[4]) if mt else False,
                    "organic_swaps": d["swaps"], "wallets": int(w), "pools": len(d["pools"]),
                    "automated_swap_share": round(d["auto"] / d["swaps"], 3) if d["swaps"] else None,
                    "self_cancelling_swaps": d["sc"],
                    "printed_share": round(d["printed"] / gross, 3) if gross else (round(d["sc"] / (d["sc"] + d["swaps"]), 3) if (d["sc"] + d["swaps"]) else None),
                    "organic_volume_usd": round(d["usd"]) if d["usd_known"] else None,
                    "new_in_window": bool(fe) and (now - fe).total_seconds() < hours * 3600, "first_seen": utc(fe),
                    "verdict": mt[5] if mt else None, "audited_at": utc(mt[6]) if mt and mt[6] else None})
    return {"hours": hours, "generated_at": now.isoformat(timespec="seconds"), "eth_usd": round(eth_usd) if eth_usd else None, "rows": out}


def get(hours: int, keyed: bool, kind: str | None = None, new_only: bool = False, limit: int = MAX_ROWS) -> dict:
    with _lock:
        lst = _lists.get(hours)
    if lst is None:
        lst = build(hours)
        with _lock: _lists[hours] = lst
    rows = lst["rows"]
    if keyed:
        if kind: rows = [r for r in rows if r["kind"] == kind]
        if new_only: rows = [r for r in rows if r["new_in_window"]]
        rows = rows[:limit]
    else:
        rows = rows[:FREE_ROWS]
    return {"hours": hours, "generated_at": lst["generated_at"], "eth_usd": lst.get("eth_usd"), "total": len(lst["rows"]), "rows": rows, "gated": not keyed,
            "note": "Ranked by swaps with self-cancelling trades removed. Volume is in USD for stablecoin-quoted pools and ETH-quoted pools priced at the chain's own ETH rate (eth_usd). Verdict is the seven-day audit when it is under a day old; otherwise open the token to run one."
                    + ("" if keyed else f" The first {FREE_ROWS} are free; a key (POST /v1/keys, free) opens the full list, filters and history.")}


def _loop():
    while True:
        try:
            for h in (24, 168):
                lst = build(h)
                with _lock: _lists[h] = lst
            # keep the top of the list audited so verdicts are on the page without a visitor waiting
            with _lock: top = [r["token"] for r in _lists[24]["rows"][:PREAUDIT]]
            for tok in top:
                try: audit_token(tok, days=7, ttl_minutes=55)
                except Exception: pass  # noqa: BLE001
                time.sleep(2)
            with _lock: _lists[24] = build(24); _lists[168] = build(168)   # pick up the fresh verdicts
        except Exception:  # noqa: BLE001
            pass
        time.sleep(REBUILD_S)


def start():
    global _started
    if _started: return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="real-list").start()
