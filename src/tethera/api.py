"""HTTP API over the crowding signal. Run: uvicorn tethera.api:app --host 127.0.0.1 --port 8790

Paths live under /v1/; the bare paths are kept as aliases. Every path except /health is rate-limited
with an in-process token bucket: anonymous callers by client address (30 requests a minute, burst 10),
keyed callers by the X-Tethera-Key header, whose limit comes from the api_keys table (only a sha256
of each key is stored). Over the limit the answer is 429 with a Retry-After header.
"""
import time, hashlib, json, os, threading, time
from datetime import datetime, timezone

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Path, Query, Request, Body
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from tethera.audit import audit_token

from . import db, signal, alerts, keys, rank, profile
from .rpc import live_rpc

HEALTH_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "health.jsonl")
ANON_RPM, ANON_BURST = 30, 10
KEY_CACHE_S = 60          # how long a key's limit is remembered before the table is read again
UNLIMITED = {"/health", "/v1/health", "/docs", "/openapi.json", "/redoc"}


@asynccontextmanager
async def _lifespan(app):
    alerts.start(); rank.start()
    async with _mcp_app.router.lifespan_context(_mcp_app):
        yield


app = FastAPI(title="Tethera", version="0.5", description="Crowding signal for automated traders on Robinhood Chain.", lifespan=_lifespan)
# the site reads the API from the browser; the API is public, so any origin may call it
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"], max_age=3600)


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
            row = conn.execute("SELECT requests_per_minute FROM api_keys WHERE key_hash = %s AND NOT disabled AND (expires_at IS NULL OR expires_at > now())", (h,)).fetchone()
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
    if request.scope["path"] == "/mcp":          # the MCP app lives under /mcp/; spare clients a redirect that drops the POST body
        request.scope["path"] = "/mcp/"; request.scope["raw_path"] = b"/mcp/"
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


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else None) or (request.client.host if request.client else "?")


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
        p_token0, p_token1 = conn.execute("SELECT token0, token1 FROM pools WHERE pool = %s", (pool_id,)).fetchone()
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
            "token_audit": [f"/v1/token/{a}" for a, k in ((p_token0, k0), (p_token1, k1)) if k not in ("stable", "eth")],
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


VERDICT_COLOURS = {"organic": "#2f7d4f", "mixed": "#6b7280", "automated": "#b45309", "automated, concentrated": "#b45309",
                   "fresh-wallet driven": "#b91c1c", "printed": "#7f1d1d", "no activity": "#9ca3af"}


@app.get("/v1/token/{address}/badge.svg", include_in_schema=True)
def token_badge(address: str = TOKEN_PATH):
    """An SVG badge with the token's seven-day verdict, for a project page or a listing. Links back to the check page."""
    a = audit_token(address, days=7)
    verdict = a["verdict"] if a else "unknown"
    right = verdict if not a else f"{verdict} · {round(100 * (a.get('automated_swap_share') or 0))}% software"
    colour = VERDICT_COLOURS.get(verdict, "#9ca3af")
    lw, rw = 118, 7 * len(right) + 16
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{lw + rw}" height="22" role="img" aria-label="Tethera: {right}">
<title>Tethera real-activity check: {right}</title>
<rect width="{lw}" height="22" rx="4" fill="#1f2937"/><rect x="{lw}" width="{rw}" height="22" fill="{colour}"/><rect x="{lw - 4}" width="8" height="22" fill="{colour}"/>
<g fill="#fff" font-family="ui-monospace, Menlo, monospace" font-size="11">
<text x="8" y="15">tethera · real activity</text><text x="{lw + 8}" y="15">{right}</text></g></svg>"""
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=1800"})


@app.get("/v1/alerts")
def herd_alerts(after_id: int = Query(0, ge=0, description="Return alerts with an id above this; 0 for the most recent"), limit: int = Query(200, ge=1, le=1000)):
    """One-sided herd minutes as they are detected, newest last. Poll with the returned last_id, or use /v1/alerts/stream."""
    return alerts.after(after_id, limit)


@app.get("/v1/alerts/stream")
async def herd_alert_stream(request: Request):
    """Server-sent events: one `herd` event per one-sided herd minute, with increasing ids. Send Last-Event-ID to resume."""
    try: after_id = int(request.headers.get("last-event-id", "0"))
    except ValueError: after_id = 0
    return StreamingResponse(alerts.astream(after_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/v1/tokens")
def real_list(request: Request, hours: int = Query(24, description="24 or 168"), kind: str | None = Query(None, pattern="^(stock|other)$"),
              new: bool = Query(False, description="Only tokens that first traded inside the window"), limit: int = Query(rank.MAX_ROWS, ge=1, le=rank.MAX_ROWS)):
    """The real list: tokens ranked by organic swaps over the last 24 hours or 7 days, with printed share, automated share, wallets and the
    audit verdict. The top ten are free; a key (X-Tethera-Key) opens the full list and the kind/new filters."""
    if hours not in (24, 168):
        raise HTTPException(422, "hours must be 24 or 168")
    key = request.headers.get("x-tethera-key")
    return rank.get(hours, keyed=bool(key and key_limit(key)), kind=kind, new_only=new, limit=limit)


def _keyed(request: Request) -> bool:
    key = request.headers.get("x-tethera-key")
    return bool(key and key_limit(key))


def _need_key(request: Request):
    if not _keyed(request):
        raise HTTPException(401, "this needs a key: POST /v1/keys gives a free one (header X-Tethera-Key)")


@app.get("/v1/tokens.csv")
def real_list_csv(request: Request, hours: int = Query(24)):
    """The full real list as CSV. Needs a key."""
    _need_key(request)
    if hours not in (24, 168): raise HTTPException(422, "hours must be 24 or 168")
    rows = rank.get(hours, keyed=True)["rows"]
    cols = ["rank", "symbol", "kind", "token", "organic_swaps", "wallets", "pools", "automated_swap_share", "printed_share", "organic_volume_usd", "new_in_window", "first_seen", "verdict"]
    body = ",".join(cols) + "\n" + "\n".join(",".join("" if r[c] is None else str(r[c]) for c in cols) for r in rows) + "\n"
    return Response(body, media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=tethera-real-list-{hours}h.csv"})


@app.get("/v1/token/{address}/history")
def token_history(request: Request, address: str = TOKEN_PATH, days: int = Query(30, ge=1, le=365)):
    """Every audit computed for this token over the last `days`: verdict and shares over time. Needs a key."""
    _need_key(request)
    with db.connect() as conn:
        rows = conn.execute("""SELECT computed_at, days, verdict, swaps, wallets, automated_swap_share, printed_share, fresh_wallet_share, top10_share, organic_volume_usd
                               FROM token_audit_history WHERE token = %s AND computed_at > now() - make_interval(days => %s) ORDER BY computed_at""", (address.lower(), days)).fetchall()
    utc = lambda t: t.astimezone(timezone.utc).isoformat(timespec="minutes")
    return {"token": address, "points": [{"at": utc(r[0]), "window_days": r[1], "verdict": r[2], "swaps": r[3], "wallets": r[4], "automated_swap_share": r[5],
                                          "printed_share": r[6], "fresh_wallet_share": r[7], "top10_share": r[8], "organic_volume_usd": float(r[9]) if r[9] is not None else None} for r in rows]}


@app.get("/v1/calm")
def wait_for_calm(pool: str = POOL, direction: str = DIRECTION, max_wait: int = Query(60, ge=0, le=120, description="Seconds to wait for the herd on your side to clear")):
    """Wait-for-calm: returns as soon as your side of the pool is not a herd, or after max_wait seconds with the crowding as it stands.
    Use it as an order parameter: call, then trade when `calm` is true. Herds on this chain usually clear within a minute."""
    deadline = time.time() + max_wait
    while True:
        c = _crowding(pool, direction, None)
        calm = c["verdict"] != "herd"
        if calm or time.time() >= deadline:
            c["calm"] = calm; c["waited_seconds"] = round(max_wait - max(0.0, deadline - time.time()), 1)
            return c
        time.sleep(5)


@app.get("/v1/wallet/{address}/exposure")
def wallet_exposure(address: str = Path(..., min_length=42, max_length=42, pattern="^0x[0-9a-fA-F]{40}$"), days: int = Query(7, ge=1, le=30)):
    """Crowding exposure of one wallet: how many of its swaps landed in a herd minute, with the herd or against it."""
    with db.connect() as conn:
        conn.execute("SET statement_timeout = '60s'")
        row = conn.execute("""
            WITH my AS (SELECT s.pool, date_trunc('minute', s.block_time) AS minute, s.direction FROM swaps s
                        WHERE lower(s.tx_from) = %s AND s.block_time > now() - make_interval(days => %s) AND NOT s.self_cancel),
                 j AS (SELECT my.*, pm.auto_wallets AS same, coalesce(o.auto_wallets, 0) AS opp
                       FROM my LEFT JOIN pool_minutes pm ON pm.pool = my.pool AND pm.minute = my.minute AND pm.direction = my.direction
                       LEFT JOIN pool_minutes o ON o.pool = my.pool AND o.minute = my.minute AND o.direction <> my.direction)
            SELECT count(*), count(*) FILTER (WHERE same >= 15 AND opp <= 0.15 * same), count(*) FILTER (WHERE opp >= 15 AND same <= 0.15 * opp),
                   count(DISTINCT pool), avg(same) FILTER (WHERE same IS NOT NULL)
            FROM j""", (address.lower(), days)).fetchone()
    n, with_herd, against_herd, pools, avg_same = row
    if not n:
        raise HTTPException(404, "no swaps for this wallet in the window")
    return {"wallet": address, "window_days": days, "swaps": n, "pools": pools,
            "swaps_inside_a_herd_same_side": with_herd, "swaps_against_a_herd": against_herd,
            "share_with_herd": round(with_herd / n, 4), "share_against_herd": round(against_herd / n, 4),
            "mean_automated_wallets_on_your_side": round(float(avg_same), 1) if avg_same is not None else None,
            "note": "A herd minute has 15+ automated wallets one way and at most 15% of that the other way. Measured price impact of herds is small in the main pools "
                    "(see the outcome study on the Method page); this tells you how often you are in one, not what it cost."}


@app.get("/v1/wallet/{address}/profile")
def wallet_profile_route(address: str = Path(..., min_length=42, max_length=42, pattern="^0x[0-9a-fA-F]{40}$")):
    """Reputation profile of any wallet: age, transaction count, labels, family, early entries into new pools, and plain-words flags."""
    out = profile.wallet_profile(address)
    if out is None:
        raise HTTPException(404, "no swaps from this address in anything we hold")
    return out


@app.get("/v1/keys/tiers")
def key_tiers():
    """Rate limits for anonymous, free-key and paid callers, and how to buy a paid key."""
    return keys.tiers()


@app.post("/v1/keys", status_code=201)
def create_key(request: Request, label: str = Body("", embed=True, max_length=64)):
    """Create a free API key (120 requests a minute, no expiry). Three a day per address. No account, no email."""
    out = keys.create_free(_client_ip(request), label)
    if out is None:
        raise HTTPException(429, "three free keys a day per address; use the ones you have, or buy a paid tier")
    return out


@app.post("/v1/keys/paid")
def create_paid_key(tx_hash: str = Body(..., embed=True), label: str = Body("", embed=True, max_length=64)):
    """Turn a USDG payment to the treasury into a 30-day key at the tier the amount buys. See /v1/keys/tiers."""
    status, body = keys.create_paid(tx_hash, label)
    if status >= 400:
        raise HTTPException(status, body["error"])
    return JSONResponse(body, status_code=status)


@app.get("/v1/keys/burn/message")
def burn_message(wallet: str = Query(..., min_length=42, max_length=42, pattern="^0x[0-9a-fA-F]{40}$")):
    """The text to sign with the wallet that burned the token. Valid for fifteen minutes."""
    minute = int(time.time() // 60)
    return {"wallet": wallet, "minute": minute, "message": keys.burn_message(wallet, minute), "token": keys.token(), "burn_to": keys.DEAD,
            "tiers": keys.BURN_TIERS, "days": keys.BURN_DAYS, "open": keys.token() is not None,
            "then": "POST /v1/keys/burn with {wallet, minute, signature}; the signature is EIP-191 personal_sign of the message"}


@app.post("/v1/keys/burn")
def create_burn_key(wallet: str = Body(..., embed=True, min_length=42, max_length=42), minute: int = Body(..., embed=True),
                    signature: str = Body(..., embed=True, min_length=130, max_length=134), label: str = Body("", embed=True, max_length=64)):
    """A 30-day key for a wallet that burned the project token: the burns are read from the chain, unspent burns pick the
    tier, and the key is non-transferable and fingerprinted. Opens when the token exists."""
    status, body = keys.create_burn(wallet, signature, minute, label)
    if status >= 400:
        raise HTTPException(status, body["error"])
    return JSONResponse(body, status_code=status)


RECORD_DELAY_S = int(os.environ.get("RECORD_DELAY_S", "900"))    # fifteen minutes until the solicitor sets the shape


@app.get("/v1/desk/record")
def desk_record(request: Request, since_id: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)):
    """What the desk did: its decision record, one row per action, hash-chained, served with a delay and fingerprinted
    per key. Needs a key. This is a record of actions taken on a paper account, not advice."""
    _need_key(request)
    with db.connect() as conn:
        rows = conn.execute("""SELECT id, at, strategy, symbol, action, size, price, reason, record_hash FROM desk_record
                               WHERE id > %s AND at <= now() - make_interval(secs => %s) ORDER BY id LIMIT %s""",
                            (since_id, RECORD_DELAY_S, limit)).fetchall()
    out = [{"id": r[0], "at": r[1].astimezone(timezone.utc).isoformat(timespec="seconds"), "strategy": r[2], "symbol": r[3], "action": r[4],
            "size": r[5], "price": r[6], "reason": r[7], "hash": r[8]} for r in rows]
    seed = keys.fingerprint_of(request.headers.get("x-tethera-key", ""))
    return {"rows": keys.fingerprint(out, seed), "delay_seconds": RECORD_DELAY_S, "next_since_id": out[-1]["id"] if out else since_id,
            "licence": "Personal or internal use; no redistribution. Rows are fingerprinted per key.",
            "note": "A record of a paper account's actions. Each hash covers the minute, strategy, symbol, action, size, reason and price to one decimal, chained to the previous row, so it verifies from this feed against the published ledger."}


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

# ---- the same tools over the Model Context Protocol, at /mcp (streamable HTTP, stateless, no key needed)
from . import mcp_server  # noqa: E402  (imports from this module; placed last so its names exist)
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
# the API is reached through a tunnel by hostname, so the library's local-host check (DNS-rebinding protection for servers on 127.0.0.1) is off
_mcp_app = mcp_server.mcp.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True,
                                               transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
app.mount("/mcp", _mcp_app)
