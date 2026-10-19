"""Herd alerts: every one-sided busy minute as it happens, for polling or as a server-sent event stream.

A watcher thread reads pool_minutes every POLL_S seconds and records each (pool, minute, direction) the first time it
qualifies as a herd (15+ automated wallets one way, the other side at most 15% of that). Events get increasing ids;
the last KEEP are held in memory. GET /v1/alerts?after_id=N returns what came after N; GET /v1/alerts/stream sends
the same as text/event-stream and honours Last-Event-ID, so a client that drops can resume without a gap.
"""
import asyncio, json, threading, time
from collections import deque
from datetime import datetime, timezone, timedelta

from . import db

POLL_S, KEEP, LOOKBACK_MIN = 15, 1000, 10
_events: deque = deque(maxlen=KEEP)
_seen: dict[tuple, float] = {}
_lock = threading.Lock()
_cond = threading.Condition(_lock)
_next_id = 1
_started = False

SQL = """
SELECT pm.pool, pm.minute, pm.direction, pm.auto_wallets, coalesce(o.auto_wallets, 0), pm.swaps, pm.wallets,
       coalesce(coalesce(t0.symbol, left(p.token0, 6)) || '/' || coalesce(t1.symbol, left(p.token1, 6)), left(pm.pool, 10))
FROM pool_minutes pm
LEFT JOIN pool_minutes o ON o.pool = pm.pool AND o.minute = pm.minute AND o.direction <> pm.direction
LEFT JOIN pools p ON p.pool = pm.pool LEFT JOIN tokens t0 ON t0.token = p.token0 LEFT JOIN tokens t1 ON t1.token = p.token1
WHERE pm.minute > %s AND pm.auto_wallets >= 15 AND coalesce(o.auto_wallets, 0) <= 0.15 * pm.auto_wallets
ORDER BY pm.minute, pm.auto_wallets DESC"""


def _poll(since):
    with db.connect() as conn:
        conn.execute("SET statement_timeout = '10s'")
        return conn.execute(SQL, (since,)).fetchall()


def _loop():
    global _next_id
    since = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MIN)
    while True:
        try:
            rows = _poll(since)
            now = time.time()
            with _cond:
                for pool, minute, direction, same, opp, swaps, wallets, pair in rows:
                    key = (pool, minute, direction)
                    if key in _seen: continue
                    _seen[key] = now
                    _events.append({"id": _next_id, "minute": minute.astimezone(timezone.utc).isoformat(timespec="minutes"),
                                    "pool": pool, "pair": pair, "direction": direction, "auto_wallets_same": int(same),
                                    "auto_wallets_opposite": int(opp), "swaps": int(swaps), "wallets": int(wallets),
                                    "seen_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
                    _next_id += 1
                for k in [k for k, t in _seen.items() if now - t > 3600]: del _seen[k]
                _cond.notify_all()
            # a minute keeps filling for a while after it starts; keep re-reading the last few so late herds are caught
            since = datetime.now(timezone.utc) - timedelta(minutes=3)
        except Exception:  # noqa: BLE001  (database hiccup: try again next tick)
            pass
        time.sleep(POLL_S)


def start():
    global _started
    if _started: return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="herd-alerts").start()


def after(after_id: int, limit: int = 200) -> dict:
    with _lock:
        evs = [e for e in _events if e["id"] > after_id][-limit:]
        last = _events[-1]["id"] if _events else 0
    return {"alerts": evs, "last_id": last, "poll_seconds": POLL_S,
            "note": "A herd is 15+ automated wallets one way in one pool in one minute with the other side at most 15% of that; volume bots and self-cancelling swaps excluded."}


def stream(after_id: int):
    """Generator of SSE frames. Sends a comment every POLL_S seconds so proxies keep the connection open."""
    yield f"retry: 5000\n: tethera herd alerts, resume from Last-Event-ID\n\n"
    cursor = after_id
    while True:
        with _cond:
            evs = [e for e in _events if e["id"] > cursor]
            if not evs: _cond.wait(timeout=POLL_S)
            evs = [e for e in _events if e["id"] > cursor]
        if not evs:
            yield ": keepalive\n\n"; continue
        for e in evs:
            cursor = e["id"]
            yield f"id: {e['id']}\nevent: herd\ndata: {json.dumps(e)}\n\n"


async def astream(after_id: int):
    """Async version for the HTTP server: polls the buffer once a second without holding a worker thread."""
    yield "retry: 5000\n: tethera herd alerts, resume from Last-Event-ID\n\n"
    cursor, quiet = after_id, 0.0
    while True:
        with _lock:
            evs = [e for e in _events if e["id"] > cursor]
        if evs:
            for e in evs:
                cursor = e["id"]
                yield f"id: {e['id']}\nevent: herd\ndata: {json.dumps(e)}\n\n"
            quiet = 0.0
        else:
            await asyncio.sleep(1.0); quiet += 1.0
            if quiet >= POLL_S:
                yield ": keepalive\n\n"; quiet = 0.0
