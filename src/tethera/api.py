"""HTTP API of the research desk and its chain data layer. Run: uvicorn tethera.api:app --host 127.0.0.1 --port 8790

Paths live under /v1/. Every path except /health is rate-limited
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
from . import db, keys, profile
from .rpc import live_rpc

HEALTH_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "health.jsonl")
ANON_RPM, ANON_BURST = 30, 10
KEY_CACHE_S = 60          # how long a key's limit is remembered before the table is read again
UNLIMITED = {"/health", "/v1/health", "/docs", "/openapi.json", "/redoc"}


app = FastAPI(title="Tethera", version="0.6", description="A verifiable systematic research desk for tokenised equities on Robinhood Chain, and the chain data layer under it.")
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


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else None) or (request.client.host if request.client else "?")


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
            "last_7_days": {"swaps": n, "peak_wallets_in_a_minute": peak_wallets_minute, "automated_swap_share": round(auto_n / n, 4) if n else None,
                            "organic_volume_usd": round(float(organic_usd), 2) if organic_usd is not None else None,
                            "self_cancelling_volume_usd": round(float(printed_usd), 2) if printed_usd is not None else None,
                            "self_cancelling_swaps": sc_n, "self_cancelling_senders": sc_w, "self_cancelling_share": round(printed_share, 4),
                            "herd_minutes": herd[0] or 0, "peak_automated_wallets_one_way": herd[1],
                            "last_swap": utc(last)},
            "flags": flags,
            "note": "Volumes exclude self-cancelling swaps (one transaction, both ways, nothing settled) unless named as such; see docs/api.md."}

@app.get("/v1/health")
@app.get("/health", include_in_schema=False)
def health():
    return _health()


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


def _keyed(request: Request) -> bool:
    key = request.headers.get("x-tethera-key")
    return bool(key and key_limit(key))


def _need_key(request: Request):
    if not _keyed(request):
        raise HTTPException(401, "this needs a key: POST /v1/keys gives a free one (header X-Tethera-Key)")


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


# ---- paper agents: registered strategies running live on the chain feed (23 Oct 2026) ----
@app.get("/v1/desk/agents")
def desk_agents():
    """The paper agents: one per registered strategy, priced from the chain's own bars. Equity and positions are live;
    the fills behind them are served with the record delay (live to key holders). Paper books, not on-chain transactions."""
    with db.connect() as conn:
        rows = conn.execute("""SELECT a.id, a.name, a.label, a.engine, a.tf, a.symbols, a.notional, a.started, a.note, a.genome,
                                      m.equity, m.at, (SELECT count(*) FROM desk_record r WHERE r.strategy = a.id)
                               FROM desk_agents a LEFT JOIN LATERAL (SELECT equity, at FROM desk_agent_marks WHERE agent_id = a.id ORDER BY at DESC LIMIT 1) m ON true
                               WHERE a.status = 'paper' ORDER BY a.started""").fetchall()
        books = conn.execute("SELECT agent_id, symbol, position FROM desk_agent_book").fetchall()
    pos = {}
    for aid, sym, p in books: pos.setdefault(aid, {})[sym] = "long" if p > 0 else "short" if p < 0 else "flat"
    out = []
    for r in rows:
        g = r[9] if isinstance(r[9], dict) else json.loads(r[9]); start = r[6] * len(r[5])
        out.append({"id": r[0], "name": r[1], "label": r[2], "engine": r[3], "timeframe": r[4], "symbols": r[5], "started": r[7].astimezone(timezone.utc).isoformat(timespec="seconds"),
                    "families": [c["family"] for c in g.get("signals", [])], "filters": [c["family"] for c in g.get("filters", [])],
                    "equity": r[10], "return": (r[10] / start - 1) if r[10] is not None else None, "marked_at": r[11].astimezone(timezone.utc).isoformat(timespec="seconds") if r[11] else None,
                    "positions": pos.get(r[0], {}), "fills": r[12], "note": r[8]})
    return {"agents": out, "paper": True, "note": "Paper books priced from on-chain bars; fills at the next chain minute's open plus a fixed slippage until the measured number replaces it. Not on-chain transactions. Not advice."}


@app.get("/v1/desk/agents/{agent_id}")
def desk_agent(request: Request, agent_id: str = Path(..., min_length=3, max_length=40, pattern="^[a-z0-9-]+$"), marks: int = Query(500, ge=1, le=5000)):
    """One agent: its equity marks, and its fills (delayed unless keyed). The strategy itself is not served here."""
    delay = 0 if _keyed(request) else RECORD_DELAY_S
    with db.connect() as conn:
        a = conn.execute("SELECT id, name, label, engine, tf, symbols, notional, started, note FROM desk_agents WHERE id = %s", (agent_id,)).fetchone()
        if not a: raise HTTPException(404, "no such agent")
        mk = conn.execute("SELECT at, equity, detail FROM desk_agent_marks WHERE agent_id = %s ORDER BY at DESC LIMIT %s", (agent_id, marks)).fetchall()
        fills = conn.execute("""SELECT id, at, symbol, action, size, price, record_hash FROM desk_record WHERE strategy = %s
                                AND at <= now() - make_interval(secs => %s) ORDER BY id""", (agent_id, delay)).fetchall()
    return {"id": a[0], "name": a[1], "label": a[2], "engine": a[3], "timeframe": a[4], "symbols": a[5], "notional_per_symbol": a[6],
            "started": a[7].astimezone(timezone.utc).isoformat(timespec="seconds"), "note": a[8],
            "marks": [{"at": m[0].astimezone(timezone.utc).isoformat(timespec="seconds"), "equity": m[1]} for m in reversed(mk)],
            "fills": [{"id": f[0], "at": f[1].astimezone(timezone.utc).isoformat(timespec="minutes"), "symbol": f[2], "action": f[3], "size": f[4], "price": round(f[5], 2), "hash": f[6]} for f in fills],
            "fills_delay_seconds": delay, "paper": True}
