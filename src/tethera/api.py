"""HTTP API over the crowding signal. Run: uvicorn tethera.api:app --host 127.0.0.1 --port 8790

Paths live under /v1/; the bare paths are kept as aliases. Every path except /health is rate-limited
with an in-process token bucket: anonymous callers by client address (30 requests a minute, burst 10),
keyed callers by the X-Tethera-Key header, whose limit comes from the api_keys table (only a sha256
of each key is stored). Over the limit the answer is 429 with a Retry-After header.
"""
import time, hashlib, json, os, threading, time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from tethera.audit import audit_token

from . import db, signal
from .rpc import live_rpc

HEALTH_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "health.jsonl")
ANON_RPM, ANON_BURST = 30, 10
KEY_CACHE_S = 60          # how long a key's limit is remembered before the table is read again
UNLIMITED = {"/health", "/v1/health", "/docs", "/openapi.json", "/redoc"}

app = FastAPI(title="Tethera", version="0.4", description="Crowding signal for automated traders on Robinhood Chain.")
# the site reads the API from the browser; the API is public and read-only, so any origin may call it
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"], max_age=3600)


class Buckets:
    """Token buckets keyed by client id, one lock, no external store."""

    def __init__(self):
        self._b: dict[str, list[float]] = {}     # id -> [tokens, last_refill]
        self._lock = threading.Lock()

    def take(self, cid: str, rpm: float, burst: float) -> float:
        """Returns 0 when a token was taken, else the seconds until one is available."""
        rate = rpm / 60.0
        with self._lock:
            now = time.monotonic()
            tokens, last = self._b.get(cid, [burst, now])
            tokens = min(burst, tokens + (now - last) * rate)
            if tokens >= 1.0:
                self._b[cid] = [tokens - 1.0, now]
                return 0.0
            self._b[cid] = [tokens, now]
            return (1.0 - tokens) / rate

    def prune(self, older_than_s: float = 3600):
        with self._lock:
            cut = time.monotonic() - older_than_s
            for k in [k for k, (_, last) in self._b.items() if last < cut]:
                del self._b[k]


buckets = Buckets()
_keys: dict[str, tuple[float, int | None]] = {}   # key hash -> (checked_at, rpm or None when unknown/disabled)
_keys_lock = threading.Lock()


def key_limit(key: str) -> int | None:
    h = hashlib.sha256(key.encode()).hexdigest()
    with _keys_lock:
        hit = _keys.get(h)
        if hit and time.monotonic() - hit[0] < KEY_CACHE_S:
            return hit[1]
    rpm = None
    try:
        with db.connect() as conn:
            row = conn.execute("SELECT requests_per_minute FROM api_keys WHERE key_hash = %s AND NOT disabled", (h,)).fetchone()
            rpm = row[0] if row else None
    except Exception:  # noqa: BLE001  (database down: treat as anonymous rather than fail)
        rpm = None
    with _keys_lock:
        _keys[h] = (time.monotonic(), rpm)
        if len(_keys) > 10_000:
            _keys.clear()
    return rpm


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if request.url.path in UNLIMITED:
        return await call_next(request)
    key = request.headers.get("x-tethera-key")
    rpm = key_limit(key) if key else None
    if rpm:
        cid, burst = "k:" + hashlib.sha256(key.encode()).hexdigest()[:16], max(10, rpm // 6)
    else:
        rpm, burst = ANON_RPM, ANON_BURST
        fwd = request.headers.get("x-forwarded-for")
        cid = "a:" + ((fwd.split(",")[0].strip() if fwd else None) or (request.client.host if request.client else "?"))
    wait = buckets.take(cid, rpm, burst)
    if wait > 0:
        retry = max(1, int(wait + 0.999))
        return JSONResponse({"error": "rate limited", "retry_after_seconds": retry, "limit_per_minute": rpm}, status_code=429,
                            headers={"Retry-After": str(retry), "X-RateLimit-Limit": str(rpm)})
    if int(time.monotonic()) % 600 == 0:
        buckets.prune()
    resp = await call_next(request)
    resp.headers["X-RateLimit-Limit"] = str(rpm)
    return resp


def _health():
    """Public liveness: how fresh the data is. Internal detail (job pids, disks, paths) stays in the healthcheck log."""
    with db.connect() as conn:
        row = conn.execute("""SELECT max(block_time), max(block_number), count(*)
                              FROM swaps WHERE block_time > now() - interval '5 minutes'""").fetchone()
    latest, latest_block, n = row
    out = {"latest_swap": latest.astimezone(timezone.utc).isoformat() if latest else None, "swaps_last_5_min": n,
           "lag_seconds": round((datetime.now(timezone.utc) - latest).total_seconds()) if latest else None,
           "lag_blocks": None, "rpc_endpoint": None}
    try:
        rpc = live_rpc(retries=2)
        head = rpc.block_number()
        out["rpc_endpoint"] = rpc.active.split("/")[2]           # hostname only; the paid URL carries a key
        out["lag_blocks"] = head - latest_block if latest_block else None
    except Exception as e:  # noqa: BLE001
        out["rpc_error"] = type(e).__name__
    try:
        with open(HEALTH_LOG, "rb") as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 8192))
            lines = f.read().splitlines()
            chk = json.loads(lines[-1]) if lines else None
        out["last_check"] = {"ok": chk.get("ok"), "problems": chk.get("problems", [])} if chk else None
    except (OSError, ValueError, AttributeError):
        out["last_check"] = None
    return out


def _crowding(pool: str, direction: str, size: float | None):
    try:
        out = signal.to_dict(signal.crowding(pool, direction))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e)[:200])
    out["size"] = size      # accepted now; a later version will say how much of the window this size is
    return out


def _herds(min_wallets: int):
    return {"herds": signal.current_herds(min_wallets)}


FINGERPRINT_NUMBERS = ["n_swaps", "median_gap", "gap_cv", "min_gap", "period_s", "fast_share", "hour_entropy",
                       "busiest_block_start", "busiest_block_share", "weekend_share", "us_hours_share", "n_pools",
                       "top_pool_share", "stock_share", "stable_share", "other_share", "known_pool_share", "n_tokens",
                       "round_amount_frac", "repeat_amount_frac", "log_size_median", "log_size_iqr", "reactive_share",
                       "round_trips_per_100", "net_flow_frac", "multi_pool_per_100"]


def _wallet(address: str):
    """Fingerprint of one wallet, or None when it has none (not automated, not registered, or too few swaps)."""
    cols = ", ".join("w." + c for c in FINGERPRINT_NUMBERS)
    with db.connect() as conn:
        row = conn.execute(f"""SELECT w.wallet, w.computed_at, w.window_start, w.window_end, w.is_automated, w.is_registered_agent,
                                      w.signals, w.archetype_id, a.label, a.description, w.family_id, w.family_size,
                                      w.nearest_agents, w.hour_hist, w.weekday_hist, {cols}
                               FROM wallet_fingerprints w LEFT JOIN fingerprint_archetypes a USING (archetype_id)
                               WHERE lower(w.wallet) = %s""", (address.lower(),)).fetchone()
    if not row:
        return None
    (wallet, computed_at, ws, we, auto, reg, signals, aid, label, desc, fid, fsize, nearest, hours, days), nums = row[:15], row[15:]
    utc = lambda t: t.astimezone(timezone.utc).isoformat(timespec="seconds")
    return {"wallet": wallet, "computed_at": utc(computed_at), "window_start": utc(ws), "window_end": utc(we),
            "is_automated": auto, "is_registered_agent": reg, "signals": signals,
            "archetype": {"id": aid, "label": label, "description": desc} if aid is not None else None,
            "family": {"id": fid, "size": fsize} if fid is not None else None,
            "nearest_agents": nearest or [], "hour_hist": hours, "weekday_hist": days,
            **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in zip(FINGERPRINT_NUMBERS, nums)}}



def _pool(pool: str):
    """Trust profile of one pool over the last 7 days: what traded, what of it was real, who was there."""
    with db.connect() as conn:
        meta = conn.execute("""
            SELECT p.pool, p.protocol, p.fee, p.hooks, p.created_block,
                   t0.symbol, t0.kind, t0.official, t0.decimals, t1.symbol, t1.kind, t1.official, t1.decimals
            FROM pools p LEFT JOIN tokens t0 ON t0.token = p.token0 LEFT JOIN tokens t1 ON t1.token = p.token1
            WHERE lower(p.pool) = %s""", (pool.lower(),)).fetchone()
        if not meta:
            return None
        pool_id, protocol, fee, hooks, created, s0, k0, o0, d0, s1, k1, o1, d1 = meta
        stable_side = 0 if k0 == "stable" else 1 if k1 == "stable" else None
        dec = (d0 if stable_side == 0 else d1) if stable_side is not None else None
        amt = "amount0" if stable_side == 0 else "amount1"
        usd = f"abs({amt}) / power(10, {dec})" if stable_side is not None and dec is not None else "NULL::numeric"
        # organic activity from the per-minute aggregates (which already exclude self-cancelling swaps);
        # the printed part from the partial index on flagged swaps; distinct wallets would need a scan, so it is per-minute here
        amt_col = "amount0_in" if stable_side == 0 else "amount1_in"
        usd_m = f"{amt_col} / power(10, {dec})" if stable_side is not None and dec is not None else "NULL::numeric"
        org = conn.execute(f"""
            SELECT coalesce(sum(swaps), 0), coalesce(sum(auto_swaps), 0), sum({usd_m}), max(wallets), max(minute)
            FROM pool_minutes WHERE pool = %s AND minute > now() - interval '7 days'""", (pool_id,)).fetchone()
        n, auto_n, organic_usd, peak_wallets_minute, last = org
        sc = conn.execute(f"""
            SELECT count(*), count(DISTINCT tx_from), sum({usd})
            FROM swaps WHERE pool = %s AND block_time > now() - interval '7 days' AND self_cancel""", (pool_id,)).fetchone()
        sc_n, sc_w, printed_usd = sc
        wallets = None
        herd = conn.execute("""
            WITH m AS (SELECT minute, direction, auto_wallets AS same,
                              coalesce((SELECT o.auto_wallets FROM pool_minutes o WHERE o.pool = pm.pool AND o.minute = pm.minute AND o.direction <> pm.direction), 0) AS opp
                       FROM pool_minutes pm WHERE pool = %s AND minute > now() - interval '7 days' AND auto_wallets >= 15)
            SELECT count(*) FILTER (WHERE opp <= 0.15 * same), max(same) FROM m""", (pool_id,)).fetchone()
    utc = lambda t: t.astimezone(timezone.utc).isoformat(timespec="seconds") if t else None
    tok = lambda sym, kind, off: {"symbol": sym, "kind": kind or "unknown", "official_stock_token": bool(off)}
    printed_share = (float(printed_usd or 0) / float((organic_usd or 0) + (printed_usd or 0))) if (organic_usd or printed_usd) else (sc_n / n if n else 0.0)
    flags = []
    if fee == 0: flags.append("zero_fee")
    if fee == 8388608: flags.append("dynamic_fee")
    if "copycat" in (k0, k1): flags.append("quotes_copycat_token")
    if sc_n and (sc_n + n) and sc_n / (sc_n + n) >= 0.5: flags.append("mostly_self_cancelling")
    if sc_w and sc_w <= 2 and printed_share >= 0.5: flags.append("single_sender_printing")
    return {"pool": pool_id, "protocol": protocol, "fee_tier": None if fee is None else ("dynamic" if fee == 8388608 else fee / 1e4),
            "hooks": hooks if hooks and int(hooks, 16) != 0 else None, "created_block": created,
            "token0": tok(s0, k0, o0), "token1": tok(s1, k1, o1), "priced_in": "USD" if stable_side is not None and dec is not None else None,
            "last_7_days": {"swaps": n, "peak_wallets_in_a_minute": peak_wallets_minute, "automated_swap_share": round(auto_n / n, 4) if n else None,
                            "organic_volume_usd": round(float(organic_usd), 2) if organic_usd is not None else None,
                            "self_cancelling_volume_usd": round(float(printed_usd), 2) if printed_usd is not None else None,
                            "self_cancelling_swaps": sc_n, "self_cancelling_senders": sc_w, "self_cancelling_share": round(printed_share, 4),
                            "herd_minutes": herd[0] or 0, "peak_automated_wallets_one_way": herd[1],
                            "last_swap": utc(last)},
            "flags": flags,
            "note": "Volumes exclude self-cancelling swaps (one transaction, both ways, nothing settled) unless named as such; see docs/api.md."}

POOL = Query(..., min_length=42, max_length=66, description="Pool address (v2/v3) or 32-byte pool id (v4)")
DIRECTION = Query(..., pattern="^(sell0|buy0)$", description="sell0 = selling token0 into the pool, buy0 = buying token0")
SIZE = Query(None, gt=0, description="Intended trade size in token0 units (optional; echoed back for now)")
MIN_WALLETS = Query(15, ge=5, le=500, description="Automated wallets on one side in the last minute")


@app.get("/v1/health")
@app.get("/health", include_in_schema=False)
def health():
    return _health()


@app.get("/v1/crowding")
@app.get("/crowding", include_in_schema=False)
def crowding(pool: str = POOL, direction: str = DIRECTION, size: float | None = SIZE):
    return _crowding(pool, direction, size)


@app.get("/v1/herds")
@app.get("/herds", include_in_schema=False)
def herds(min_wallets: int = MIN_WALLETS):
    return _herds(min_wallets)


@app.get("/v1/wallet/{address}")
def wallet(address: str = Path(..., min_length=42, max_length=42, pattern="^0x[0-9a-fA-F]{40}$", description="Wallet address")):
    out = _wallet(address)
    if out is None:
        raise HTTPException(404, "no fingerprint for this wallet: not labelled automated or registered, or too few swaps in the window")
    return out

POOL_PATH = Path(..., min_length=42, max_length=66, pattern="^0x[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$", description="Pool address (v2/v3) or 32-byte pool id (v4)")


@app.get("/v1/pool/{pool}")
def pool_profile(pool: str = POOL_PATH):
    out = _pool(pool)
    if out is None:
        raise HTTPException(status_code=404, detail="unknown pool")
    return out


TOKEN_PATH = Path(..., min_length=42, max_length=42, pattern="^0x[0-9a-fA-F]{40}$", description="Token contract address")
DAYS = Query(7, ge=1, le=30, description="Window in days")


@app.get("/v1/token/{address}")
def token_audit(address: str = TOKEN_PATH, days: float = DAYS):
    """Launch audit for one token: how much of its activity was real, who was there, and a verdict. Cached for 30 minutes."""
    out = audit_token(address, days=days)
    if out is None:
        raise HTTPException(status_code=404, detail="unknown token")
    return out


# ---- series and the live flock, for the site's drawings. Cached in-process: these aggregate a million rows.
_series_cache = {}

def _cached(key, ttl, fn):
    now = time.time(); hit = _series_cache.get(key)
    if hit and now - hit[0] < ttl: return hit[1]
    val = fn(); _series_cache[key] = (now, val); return val

def _series(hours: int):
    def build():
        with db.connect() as conn:
            conn.execute("SET work_mem = '256MB'"); conn.execute("SET statement_timeout = '60s'")
            rows = conn.execute("""
                WITH m AS (SELECT date_trunc('hour', minute) AS h, pool, minute, direction, swaps, auto_swaps, auto_wallets
                           FROM pool_minutes WHERE minute >= date_trunc('hour', now()) - make_interval(hours => %s)),
                     herd AS (SELECT a.h, count(*) AS herd_minutes, max(a.auto_wallets) AS largest
                              FROM m a LEFT JOIN m b ON b.pool = a.pool AND b.minute = a.minute AND b.direction <> a.direction
                              WHERE a.auto_wallets >= 15 AND coalesce(b.auto_wallets, 0) <= 0.15 * a.auto_wallets GROUP BY 1)
                SELECT m.h, sum(m.swaps), sum(m.auto_swaps), count(DISTINCT m.pool), coalesce(max(herd.herd_minutes), 0), coalesce(max(herd.largest), 0)
                FROM m LEFT JOIN herd ON herd.h = m.h GROUP BY m.h ORDER BY m.h""", (hours,)).fetchall()
            printed = conn.execute("""
                SELECT date_trunc('hour', block_time) AS h, count(*) FILTER (WHERE self_cancel), count(*)
                FROM swaps WHERE block_time >= date_trunc('hour', now()) - make_interval(hours => %s) GROUP BY 1 ORDER BY 1""", (hours,)).fetchall()
        pr = {h: (sc, n) for h, sc, n in printed}
        return {"hours": hours, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "points": [{"hour": h.astimezone(timezone.utc).isoformat(timespec="minutes"), "swaps": int(sw), "automated_share": round(a / sw, 4) if sw else None,
                            "pools": int(pools), "herd_minutes": int(hm), "largest_herd": int(lg),
                            "self_cancelling_share": round(pr[h][0] / pr[h][1], 4) if h in pr and pr[h][1] else None}
                           for h, sw, a, pools, hm, lg in rows]}
    return _cached(f"series:{hours}", 300, build)

def _flock():
    """Every pool-side with automated wallets in the last 60 seconds: the flock as it stands."""
    def build():
        with db.connect() as conn:
            conn.execute("SET statement_timeout = '20s'")
            rows = conn.execute("""
                SELECT s.pool, s.direction, count(DISTINCT s.tx_from) FILTER (WHERE s.is_automated AND NOT coalesce(f.is_volume_bot, false)) AS auto,
                       count(DISTINCT s.tx_from) AS all_wallets, coalesce(coalesce(t0.symbol, left(p.token0, 6)) || '/' || coalesce(t1.symbol, left(p.token1, 6)), left(s.pool, 10)) AS pair
                FROM swaps s LEFT JOIN wallet_features f ON f.wallet = s.tx_from AND f.is_volume_bot
                LEFT JOIN pools p ON p.pool = s.pool LEFT JOIN tokens t0 ON t0.token = p.token0 LEFT JOIN tokens t1 ON t1.token = p.token1
                WHERE s.block_time > now() - interval '60 seconds' AND NOT s.self_cancel
                GROUP BY 1, 2, 5 HAVING count(DISTINCT s.tx_from) FILTER (WHERE s.is_automated AND NOT coalesce(f.is_volume_bot, false)) > 0
                ORDER BY 3 DESC LIMIT 60""").fetchall()
        sides = {}
        for pool, d, auto, allw, pair in rows: sides.setdefault(pool, {"pool": pool, "pair": pair, "buy0": 0, "sell0": 0, "wallets": 0}); sides[pool][d] = int(auto); sides[pool]["wallets"] = max(sides[pool]["wallets"], int(allw))
        out = []
        for v in sides.values():
            same, opp = max(v["buy0"], v["sell0"]), min(v["buy0"], v["sell0"])
            v["verdict"] = "herd" if same >= 15 and opp <= 0.15 * same else "busy" if same >= 15 else "calm"; out.append(v)
        return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "window_seconds": 60,
                "automated_wallets": sum(max(v["buy0"], v["sell0"]) for v in out), "pools": out}
    return _cached("flock", 20, build)


@app.get("/v1/series")
def series(hours: int = Query(48, ge=6, le=168)):
    """Per-hour swaps, automated share, herd minutes, largest herd and self-cancelling share. Cached five minutes."""
    return _series(hours)


@app.get("/v1/flock")
def flock():
    """Automated wallets per pool side in the last 60 seconds. Cached 20 seconds."""
    return _flock()


# keep the 48-hour series warm so no visitor waits for the aggregate
def _warm_series():
    import threading
    def loop():
        while True:
            try: _series_cache.pop("series:48", None); _series(48)
            except Exception: pass
            time.sleep(240)
    threading.Thread(target=loop, daemon=True).start()

_warm_series()
