"""HTTP API over the crowding signal. Run: uvicorn tethera.api:app --host 127.0.0.1 --port 8790

Paths live under /v1/; the bare paths are kept as aliases. Every path except /health is rate-limited
with an in-process token bucket: anonymous callers by client address (30 requests a minute, burst 10),
keyed callers by the X-Tethera-Key header, whose limit comes from the api_keys table (only a sha256
of each key is stored). Over the limit the answer is 429 with a Retry-After header.
"""
import hashlib, json, os, threading, time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse

from . import db, signal
from .rpc import live_rpc

HEALTH_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "health.jsonl")
ANON_RPM, ANON_BURST = 30, 10
KEY_CACHE_S = 60          # how long a key's limit is remembered before the table is read again
UNLIMITED = {"/health", "/v1/health", "/docs", "/openapi.json", "/redoc"}

app = FastAPI(title="Tethera", version="0.3", description="Crowding signal for automated traders on Robinhood Chain.")


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
