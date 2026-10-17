"""Minimal JSON-RPC client for Robinhood Chain with throttling, retries and batching.

The public endpoint rejects Python's default user agent, returns HTTP 429 under
load, caps eth_getLogs at 10,000 matching logs and caps topic-only log queries
at 30,000 blocks. Everything here exists to live inside those limits.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

DEFAULT_RPC = "https://rpc.mainnet.chain.robinhood.com"
HEADERS = {"content-type": "application/json", "user-agent": "tethera/0.1"}


class RateLimiter:
    """Thread-safe token bucket shared by several Rpc instances: at most `rps` requests per second
    across all of them. Batch sub-requests count individually, as providers bill them."""

    def __init__(self, rps: float, burst: float | None = None, per_item: bool = True):
        self.rps = float(rps)
        self.per_item = per_item  # True: batch sub-requests each cost a token (paid providers bill that way)
        self.capacity = float(burst if burst is not None else max(1.0, rps / 4))
        self._tokens = self.capacity
        self._t = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, n: int = 1) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._t) * self.rps)
                self._t = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                wait = (n - self._tokens) / self.rps
            time.sleep(wait)


class QuotaExhausted(Exception):
    """The endpoint's plan quota is used up (the paid provider reports it as JSON-RPC error -32005 mentioning 'quota')."""


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Rpc:
    def __init__(self, url: str | None = None, min_interval: float = 0.25, retries: int = 30,
                 limiter: RateLimiter | None = None):
        self.url = url or os.environ.get("RPC_URL", DEFAULT_RPC)
        self.min_interval = min_interval
        self.retries = retries
        self.limiter = limiter  # when set, replaces min_interval with a shared budget
        self._last = 0.0
        self.throttled = 0

    def _post(self, payload) -> object:
        for attempt in range(self.retries):
            if self.limiter is not None:
                self.limiter.acquire(len(payload) if (isinstance(payload, list) and self.limiter.per_item) else 1)
            else:
                wait = self.min_interval - (time.time() - self._last)
                if wait > 0:
                    time.sleep(wait)
            req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=HEADERS)
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    self._last = time.time()
                    out = json.load(resp)
                items = out if isinstance(out, list) else [out]
                limited = [o for o in items if isinstance(o, dict) and o.get("error", {}).get("code") == -32005]
                if limited and "quota" in str(limited[0]["error"].get("message", "")).lower():
                    raise QuotaExhausted(self.url)        # a monthly cap, not a momentary limit: let the caller fail over
                if limited and attempt < self.retries - 1:
                    data = limited[0]["error"].get("data") or {}
                    hint = str(data.get("try_again_in", "")).replace("ms", "").replace("s", "")
                    wait_s = (float(hint) / 1000 if "ms" in str(data.get("try_again_in", "")) else float(hint)) if hint.replace(".", "", 1).isdigit() else 1.0
                    self.throttled += 1
                    time.sleep(max(wait_s, 0.2) + 0.1)
                    continue
                return out
            except urllib.error.HTTPError as e:
                self._last = time.time()
                if e.code in (429, 502, 503, 504) and attempt < self.retries - 1:
                    ra = e.headers.get("Retry-After") if e.headers else None
                    wait_s = float(ra) if ra and ra.replace(".", "", 1).isdigit() else min(2.0 * 2 ** min(attempt, 5), 60)
                    self.throttled += 1
                    if self.throttled % 10 == 1:
                        print(f"[rpc] HTTP {e.code}, retry-after={ra}, sleeping {wait_s:.0f}s (throttled {self.throttled}x)", flush=True)
                    time.sleep(wait_s)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt < self.retries - 1:
                    time.sleep(min(1.5 * 2 ** attempt, 45))
                    continue
                raise
        raise RuntimeError("unreachable")

    def call(self, method: str, params: list) -> object:
        out = self._post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in out:
            raise RpcError(out["error"].get("code", 0), out["error"].get("message", ""))
        return out["result"]

    def batch(self, calls: list[tuple[str, list]]) -> list[object]:
        """Send several calls in one HTTP request. Returns results in order; raises on any error."""
        if not calls:
            return []
        payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
        out = self._post(payload)
        if not isinstance(out, list):
            raise RpcError(0, f"batch reply was not a list: {str(out)[:200]}")
        by_id = {o["id"]: o for o in out}
        results = []
        for i in range(len(calls)):
            o = by_id.get(i)
            if o is None or "error" in o:
                err = (o or {}).get("error", {"code": 0, "message": "missing reply"})
                raise RpcError(err.get("code", 0), err.get("message", ""))
            results.append(o["result"])
        return results

    # -- convenience -------------------------------------------------------

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def block(self, number: int, full_txs: bool = False) -> dict:
        return self.call("eth_getBlockByNumber", [hex(number), full_txs])

    def get_logs(self, from_block: int, to_block: int, address=None, topics=None) -> list[dict]:
        """eth_getLogs over [from_block, to_block], splitting the range whenever the
        node says the query matched too many logs or spanned too many blocks."""
        flt: dict = {"fromBlock": hex(from_block), "toBlock": hex(to_block)}
        if address:
            flt["address"] = address
        if topics:
            flt["topics"] = topics
        try:
            return self.call("eth_getLogs", [flt])
        except RpcError as e:
            msg = e.message.lower()
            if ("exceeds limit" in msg or "only" in msg and "allowed" in msg) and to_block > from_block:
                mid = (from_block + to_block) // 2
                return self.get_logs(from_block, mid, address, topics) + self.get_logs(mid + 1, to_block, address, topics)
            raise

    def eth_call(self, to: str, data: str, block: str = "latest") -> str:
        return self.call("eth_call", [{"to": to, "data": data}, block])

class FailoverRpc(Rpc):
    """An Rpc over several endpoints. Requests go to the first healthy endpoint; one that
    answers 401/403 (quota or key problems) or exhausts its retries is marked down and
    retried after `cooldown` seconds. Each endpoint keeps its own pacing."""

    def __init__(self, endpoints: list[tuple[str, float]], retries: int = 6, cooldown: float = 600.0):
        # endpoints: [(url, min_interval_seconds), ...] in order of preference
        super().__init__(endpoints[0][0], min_interval=endpoints[0][1], retries=retries)
        self.endpoints = endpoints
        self.cooldown = cooldown
        self._down: dict[str, float] = {}       # url -> time it went down
        self._last_by_url: dict[str, float] = {}
        self.active = endpoints[0][0]

    def _healthy(self) -> list[tuple[str, float]]:
        now = time.time()
        up = [(u, i) for u, i in self.endpoints if now - self._down.get(u, -1e12) > self.cooldown]
        return up or list(self.endpoints)       # if all are down, try them all anyway

    def _post(self, payload) -> object:
        last_err: Exception | None = None
        for url, interval in self._healthy():
            self.url, self.min_interval = url, interval
            self._last = self._last_by_url.get(url, 0.0)
            try:
                out = super()._post(payload)
                self._last_by_url[url] = self._last
                if url != self.active:
                    print(f"[rpc] switched to {url.split('/')[2]}", flush=True)
                    self.active = url
                return out
            except QuotaExhausted as e:
                self._last_by_url[url] = self._last
                last_err = e
                self._down[url] = time.time()
                print(f"[rpc] {url.split('/')[2]} quota exhausted; marked down for {self.cooldown:.0f}s", flush=True)
                continue
            except urllib.error.HTTPError as e:
                self._last_by_url[url] = self._last
                last_err = e
                if e.code in (401, 403, 402, 429, 502, 503, 504):
                    self._down[url] = time.time()
                    print(f"[rpc] {url.split('/')[2]} returned HTTP {e.code}; marked down for {self.cooldown:.0f}s", flush=True)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                self._last_by_url[url] = self._last
                last_err = e
                self._down[url] = time.time()
                print(f"[rpc] {url.split('/')[2]} unreachable ({type(e).__name__}); marked down for {self.cooldown:.0f}s", flush=True)
                continue
        assert last_err is not None
        raise last_err


def live_rpc(retries: int = 6) -> Rpc:
    """The follower's RPC: the paid endpoint first (if configured), then the public one."""
    eps: list[tuple[str, float]] = []
    if os.environ.get("PAID_RPC_URL"):
        eps.append((os.environ["PAID_RPC_URL"], 0.08))     # ~12 req/s, under the plan's 25 rps cap
    eps.append((os.environ.get("RPC_URL", DEFAULT_RPC), 0.25))    # public endpoint: gentle
    return FailoverRpc(eps, retries=retries)
