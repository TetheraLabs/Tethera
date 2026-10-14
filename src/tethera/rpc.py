"""Minimal JSON-RPC client for Robinhood Chain with throttling, retries and batching.

The public endpoint rejects Python's default user agent, returns HTTP 429 under
load, caps eth_getLogs at 10,000 matching logs and caps topic-only log queries
at 30,000 blocks. Everything here exists to live inside those limits.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_RPC = "https://rpc.mainnet.chain.robinhood.com"
HEADERS = {"content-type": "application/json", "user-agent": "tethera/0.1"}


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Rpc:
    def __init__(self, url: str | None = None, min_interval: float = 0.25, retries: int = 30):
        self.url = url or os.environ.get("RPC_URL", DEFAULT_RPC)
        self.min_interval = min_interval
        self.retries = retries
        self._last = 0.0
        self.throttled = 0

    def _post(self, payload) -> object:
        for attempt in range(self.retries):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=HEADERS)
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    self._last = time.time()
                    return json.load(resp)
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
