"""Token audit as a library: the launch audit's measurements for one token, with a short cache, for the API and the site.

For a token address over a window (default 7 days) it returns what scripts/launch_audit.py reports for one row, plus the
pools involved. Results are cached in token_audits for `ttl` minutes because the query walks every swap in the token's
pools. The on-chain transaction counts (born-for-this) are read from wallet_nonce when present and fetched for at most
`nonce_max` wallets when `fetch_nonces` is set; the API leaves fetching to the nightly job and reports what is cached.
"""
import os, time, json
from decimal import Decimal
from datetime import datetime, timezone, timedelta
import psycopg

DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")

META = """
SELECT t.token, t.symbol, t.name, t.kind, t.official, t.decimals FROM tokens t WHERE lower(t.token) = %(tok)s
"""
POOLS = """
SELECT p.pool, p.protocol, p.fee,
       CASE WHEN lower(p.token0) = %(tok)s THEN t1.symbol ELSE t0.symbol END AS counter_symbol,
       CASE WHEN lower(p.token0) = %(tok)s THEN t1.kind ELSE t0.kind END AS counter_kind,
       coalesce(sum(m.swaps), 0) AS swaps
FROM pools p LEFT JOIN tokens t0 ON t0.token = p.token0 LEFT JOIN tokens t1 ON t1.token = p.token1
LEFT JOIN pool_minutes m ON m.pool = p.pool AND m.minute >= %(since)s
WHERE lower(p.token0) = %(tok)s OR lower(p.token1) = %(tok)s
GROUP BY 1, 2, 3, 4, 5 ORDER BY 6 DESC
"""
AUDIT = """
WITH pl AS (
    SELECT p.pool, p.fee,
           CASE WHEN t0.kind = 'stable' THEN 0 WHEN t1.kind = 'stable' THEN 1 END AS stable_side,
           CASE WHEN t0.kind = 'stable' THEN t0.decimals ELSE t1.decimals END AS dec,
           CASE WHEN lower(p.token0) = %(tok)s THEN 0 ELSE 1 END AS tok_side
    FROM pools p LEFT JOIN tokens t0 ON t0.token = p.token0 LEFT JOIN tokens t1 ON t1.token = p.token1
    WHERE lower(p.token0) = %(tok)s OR lower(p.token1) = %(tok)s
), sw AS (
    SELECT s.tx_from AS w, s.block_time, s.is_automated, s.self_cancel, pl.tok_side,
           CASE WHEN pl.stable_side = 0 THEN abs(s.amount0) / power(10, pl.dec) WHEN pl.stable_side = 1 THEN abs(s.amount1) / power(10, pl.dec) END AS usd,
           CASE WHEN (pl.tok_side = 0 AND s.direction = 'buy0') OR (pl.tok_side = 1 AND s.direction = 'sell0') THEN 'buy' ELSE 'sell' END AS side
    FROM swaps s JOIN pl ON pl.pool = s.pool WHERE s.block_time >= %(since)s
), tot AS (
    SELECT count(*) AS swaps, count(DISTINCT w) AS wallets, min(block_time) AS first_seen, max(block_time) AS last_seen,
           count(*) FILTER (WHERE is_automated AND NOT self_cancel) AS auto_swaps, count(*) FILTER (WHERE self_cancel) AS sc_swaps,
           sum(usd) FILTER (WHERE NOT self_cancel) AS organic_usd, sum(usd) FILTER (WHERE self_cancel) AS printed_usd,
           count(DISTINCT w) FILTER (WHERE is_automated) AS auto_wallets
    FROM sw
), perw AS (
    SELECT w, count(*) AS n, bool_or(side = 'buy') AS bought, bool_or(side = 'sell') AS sold FROM sw WHERE NOT self_cancel GROUP BY w
), first_any AS (
    SELECT s.tx_from AS w, min(s.block_time) AS first_any, count(*) AS n_any FROM swaps s WHERE s.tx_from IN (SELECT w FROM perw) GROUP BY 1
), joined AS (
    SELECT perw.*, fa.first_any, fa.n_any, (SELECT first_seen FROM tot) AS t0, n.nonce
    FROM perw JOIN first_any fa USING (w) LEFT JOIN wallet_nonce n ON n.wallet = perw.w
), fresh AS (SELECT w, first_any FROM joined WHERE first_any >= t0),
born AS (
    SELECT date_trunc('hour', first_any) + make_interval(mins => (extract(minute FROM first_any)::int / 10) * 10) AS bucket, count(*) AS n FROM fresh GROUP BY 1
), top10 AS (SELECT sum(n) AS n FROM (SELECT n FROM perw ORDER BY n DESC LIMIT 10) x)
SELECT tot.swaps, tot.wallets, tot.first_seen, tot.last_seen, tot.auto_swaps, tot.auto_wallets, tot.sc_swaps, tot.organic_usd, tot.printed_usd,
       (SELECT count(*) FROM fresh), (SELECT count(*) FROM joined WHERE n_any = 1),
       (SELECT max(n) FROM born), (SELECT bucket FROM born ORDER BY n DESC LIMIT 1),
       (SELECT n FROM top10), (SELECT count(*) FROM joined WHERE bought AND NOT sold), (SELECT count(*) FROM joined WHERE sold AND NOT bought),
       (SELECT count(*) FROM joined),
       (SELECT count(*) FROM joined WHERE nonce IS NOT NULL), (SELECT count(*) FROM joined WHERE nonce IS NOT NULL AND nonce <= n + 2),
       (SELECT count(*) FROM joined WHERE nonce IS NOT NULL AND nonce <= 2)
FROM tot
"""

def verdict(r, new_token):
    """From the shares in the result dict."""
    if not r["swaps"]: return "no activity"
    printed = r["printed_share"]; auto = r["automated_swap_share"] or 0
    fresh = r["fresh_wallet_share"] or 0; one = r["one_swap_wallet_share"] or 0
    born = (r["born_together"] or 0) / (r["organic_wallets"] or 1)
    top10 = r["top10_share"] or 0; bft = r["born_for_this_share"]
    if printed is not None and printed >= 0.5: return "printed"
    if auto >= 0.6 and top10 >= 0.5: return "automated, concentrated"
    if auto >= 0.6: return "automated"
    if new_token and (fresh >= 0.8 or (bft is not None and bft >= 0.5)): return "fresh-wallet driven"
    if fresh >= 0.6 and (one >= 0.4 or born >= 0.25): return "fresh-wallet driven"
    if auto < 0.35 and top10 < 0.3 and (fresh < 0.6 or not new_token): return "organic"
    return "mixed"

def ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS token_audits (
        token VARCHAR(42) NOT NULL, days REAL NOT NULL, computed_at TIMESTAMPTZ NOT NULL DEFAULT now(), result JSONB NOT NULL,
        PRIMARY KEY (token, days))""")

def audit_token(address: str, days: float = 7, ttl_minutes: int = 30, conn=None) -> dict | None:
    """The audit for one token, or None when the token is unknown to us. Cached for ttl_minutes."""
    tok = address.lower(); own = conn is None
    if own: conn = psycopg.connect(DB)
    try:
        ensure_table(conn)
        row = conn.execute("SELECT result, computed_at FROM token_audits WHERE token = %s AND days = %s AND computed_at > now() - make_interval(mins => %s)",
                           (tok, days, ttl_minutes)).fetchone()
        if row:
            out = row[0]; out["cached"] = True; return out
        meta = conn.execute(META, {"tok": tok}).fetchone()
        since = datetime.now(timezone.utc) - timedelta(days=days)
        pools = conn.execute(POOLS, {"tok": tok, "since": since}).fetchall()
        if not meta and not pools: return None
        conn.execute("SET work_mem = '256MB'"); conn.execute("SET statement_timeout = '5min'")
        cols = ["swaps", "wallets", "first_seen", "last_seen", "auto_swaps", "auto_wallets", "sc_swaps", "organic_usd", "printed_usd",
                "fresh_wallets", "one_swap_wallets", "born_together", "born_bucket", "top10_swaps", "only_bought", "only_sold", "organic_wallets",
                "nonce_known", "born_for_this", "nonce_le2"]
        r = dict(zip(cols, conn.execute(AUDIT, {"tok": tok, "since": since}).fetchone()))
        org = (r["swaps"] or 0) - (r["sc_swaps"] or 0); ow = r["organic_wallets"] or 0
        gross = (r["organic_usd"] or 0) + (r["printed_usd"] or 0)
        utc = lambda t: t.astimezone(timezone.utc).isoformat(timespec="seconds") if t else None
        new_token = bool(r["first_seen"]) and (r["first_seen"] - since).total_seconds() > 600
        out = {
            "token": meta[0] if meta else address, "symbol": meta[1] if meta else None, "name": meta[2] if meta else None,
            "kind": (meta[3] if meta else None) or "unknown", "official_stock_token": bool(meta[4]) if meta else False,
            "window_days": days, "first_seen": utc(r["first_seen"]), "last_seen": utc(r["last_seen"]), "new_in_window": new_token,
            "pools": [{"pool": p[0], "protocol": p[1], "fee_tier": ("dynamic" if p[2] == 8388608 else (p[2] / 1e4 if p[2] is not None else None)),
                       "against": p[3], "against_kind": p[4], "swaps": int(p[5])} for p in pools[:20]],
            "swaps": r["swaps"], "wallets": r["wallets"], "organic_wallets": ow,
            "automated_swap_share": round(r["auto_swaps"] / org, 4) if org else None,
            "automated_wallet_share": round(r["auto_wallets"] / r["wallets"], 4) if r["wallets"] else None,
            "self_cancelling_swaps": r["sc_swaps"], "self_cancelling_share": round(r["sc_swaps"] / r["swaps"], 4) if r["swaps"] else None,
            "organic_volume_usd": round(float(r["organic_usd"]), 2) if r["organic_usd"] is not None else None,
            "printed_volume_usd": round(float(r["printed_usd"]), 2) if r["printed_usd"] is not None else None,
            "printed_share": round(float(r["printed_usd"] or 0) / gross, 4) if gross else None,
            "fresh_wallets": r["fresh_wallets"] if new_token else None, "fresh_wallet_share": round(r["fresh_wallets"] / ow, 4) if (ow and new_token) else None,
            "one_swap_wallet_share": round(r["one_swap_wallets"] / ow, 4) if ow else None,
            "born_together": r["born_together"] if new_token else None, "born_together_at": utc(r["born_bucket"]) if new_token else None,
            "top10_share": round(r["top10_swaps"] / org, 4) if org else None,
            "only_bought_share": round(r["only_bought"] / ow, 4) if ow else None, "only_sold_share": round(r["only_sold"] / ow, 4) if ow else None,
            "nonce_known_wallets": r["nonce_known"],
            "born_for_this_share": round(r["born_for_this"] / r["nonce_known"], 4) if r["nonce_known"] else None,
            "nonce_le2_share": round(r["nonce_le2"] / r["nonce_known"], 4) if r["nonce_known"] else None,
            "computed_at": utc(datetime.now(timezone.utc)), "cached": False,
            "note": "Shares are of organic (non-self-cancelling) swaps or wallets. Fresh and born-together are reported only for tokens whose first trade "
                    "falls inside the window and are measured since 10 Oct 2026. born_for_this = wallets whose on-chain transaction count fits inside the "
                    "swaps we saw them make here plus two; reported when transaction counts are cached for this token's wallets.",
        }
        out = {k: (float(v) if isinstance(v, Decimal) else v) for k, v in out.items()}
        out["verdict"] = verdict(out, new_token)
        conn.execute("INSERT INTO token_audits (token, days, result) VALUES (%s, %s, %s) ON CONFLICT (token, days) DO UPDATE SET result = EXCLUDED.result, computed_at = now()",
                     (tok, days, json.dumps(out, default=float))); conn.commit()
        return out
    finally:
        if own: conn.close()
