"""API keys: free keys anyone can create, and paid keys bought with a USDG transfer on Robinhood Chain.

A key is a random 32-byte token shown once; only its sha256 is stored (api_keys.key_hash). Free keys lift the
anonymous limit to FREE_RPM. A paid key is created by sending USDG to the Tethera treasury and posting the
transaction hash: the receipt is read from the chain, the Transfer to the treasury is matched to a tier, the key
gets that tier's limit for 30 days, and the hash is recorded so it cannot be used twice. Paid tiers open when
TREASURY_ADDRESS is set.
"""
import hashlib, os, secrets, threading, time
from datetime import datetime, timezone, timedelta

from . import db
from .rpc import live_rpc

FREE_RPM = 120
FREE_PER_IP_PER_DAY = 3
PAID_DAYS = 30
# USDG sent → requests per minute. Proposal; prices are a product decision and may change before launch.
TIERS = [{"name": "agent", "usdg": 10, "rpm": 600}, {"name": "desk", "usdg": 50, "rpm": 3000}]
USDG = os.environ.get("USDG_ADDRESS", "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"); USDG_DECIMALS = 6
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

_ip_log: dict[str, list[float]] = {}
_lock = threading.Lock()


def treasury() -> str | None:
    t = os.environ.get("TREASURY_ADDRESS", "").strip()
    return t if t.startswith("0x") and len(t) == 42 else None


def _new_key(label: str, rpm: int, expires: datetime | None) -> dict:
    key = "tk_" + secrets.token_urlsafe(32)
    h = hashlib.sha256(key.encode()).hexdigest()
    with db.connect() as conn:
        conn.execute("INSERT INTO api_keys (key_hash, label, requests_per_minute, expires_at) VALUES (%s, %s, %s, %s)", (h, label[:64], rpm, expires))
        conn.commit()
    return {"key": key, "requests_per_minute": rpm, "expires_at": expires.isoformat(timespec="seconds") if expires else None,
            "header": "X-Tethera-Key", "note": "Shown once. Only a hash is stored; a lost key cannot be recovered, make another."}


def create_free(client_ip: str, label: str) -> dict | None:
    """None when this address has made its daily allowance."""
    now = time.time()
    with _lock:
        hist = [t for t in _ip_log.get(client_ip, []) if now - t < 86400]
        if len(hist) >= FREE_PER_IP_PER_DAY:
            return None
        hist.append(now); _ip_log[client_ip] = hist
        if len(_ip_log) > 50_000: _ip_log.clear()
    return _new_key(label or "free", FREE_RPM, None)


def tiers() -> dict:
    return {"treasury": treasury(), "token": "USDG", "token_address": USDG, "chain_id": 4663, "days": PAID_DAYS,
            "tiers": TIERS, "free": {"anonymous_rpm": 30, "key_rpm": FREE_RPM, "keys_per_day": FREE_PER_IP_PER_DAY},
            "open": treasury() is not None,
            "how": "Send the tier's USDG to the treasury on Robinhood Chain, then POST /v1/keys/paid with {\"tx_hash\": ...}. The key lasts 30 days from the payment."}


def create_paid(tx_hash: str, label: str) -> tuple[int, dict]:
    """(status, body). Verifies the USDG transfer on-chain; never trusts the caller's amount."""
    to = treasury()
    if not to:
        return 503, {"error": "paid keys are not open yet; free keys are at POST /v1/keys"}
    tx_hash = tx_hash.strip().lower()
    if not (tx_hash.startswith("0x") and len(tx_hash) == 66):
        return 422, {"error": "tx_hash must be a 32-byte transaction hash"}
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM key_payments WHERE tx_hash = %s", (tx_hash,)).fetchone():
            return 409, {"error": "this transaction has already bought a key"}
    try:
        rcpt = live_rpc(retries=3).call("eth_getTransactionReceipt", [tx_hash])
    except Exception as e:  # noqa: BLE001
        return 502, {"error": f"could not read the chain: {type(e).__name__}"}
    if not rcpt:
        return 404, {"error": "transaction not found; wait for it to be mined and try again"}
    if int(rcpt.get("status", "0x0"), 16) != 1:
        return 422, {"error": "transaction failed on-chain"}
    paid = 0
    for lg in rcpt.get("logs", []):
        if lg["address"].lower() == USDG.lower() and lg["topics"] and lg["topics"][0].lower() == TRANSFER and len(lg["topics"]) == 3 \
           and ("0x" + lg["topics"][2][-40:]).lower() == to.lower():
            paid += int(lg["data"], 16)
    usdg = paid / 10 ** USDG_DECIMALS
    tier = max((t for t in TIERS if usdg >= t["usdg"]), key=lambda t: t["usdg"], default=None)
    if not tier:
        return 422, {"error": f"no USDG transfer to the treasury found in this transaction (saw {usdg:.2f} USDG); tiers start at {TIERS[0]['usdg']} USDG"}
    expires = datetime.now(timezone.utc) + timedelta(days=PAID_DAYS)
    out = _new_key(label or tier["name"], tier["rpm"], expires)
    with db.connect() as conn:
        conn.execute("INSERT INTO key_payments (tx_hash, key_hash, payer, usdg, tier) VALUES (%s, %s, %s, %s, %s)",
                     (tx_hash, hashlib.sha256(out["key"].encode()).hexdigest(), rcpt.get("from"), usdg, tier["name"]))
        conn.commit()
    out["tier"] = tier["name"]; out["usdg_received"] = usdg
    return 201, out
