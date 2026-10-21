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
DEAD = "0x000000000000000000000000000000000000dEaD"
# Burn access: burning the project token to the dead address buys a tier for BURN_DAYS. Opens when the token exists
# (TETHERA_TOKEN_ADDRESS in the environment). Amounts are whole tokens; a proposal until the token is priced.
BURN_TIERS = [{"name": "agent", "tokens": 100, "rpm": 600}, {"name": "desk", "tokens": 500, "rpm": 3000}]
BURN_DAYS = 30
MESSAGE_WINDOW_S = 900

_ip_log: dict[str, list[float]] = {}
_lock = threading.Lock()


def treasury() -> str | None:
    t = os.environ.get("TREASURY_ADDRESS", "").strip()
    return t if t.startswith("0x") and len(t) == 42 else None


def _new_key(label: str, rpm: int, expires: datetime | None, conn=None) -> dict:
    """Inserts the key; with `conn` given it joins that transaction and does not commit. Every key gets a fingerprint seed."""
    key = "tk_" + secrets.token_urlsafe(32)
    h = hashlib.sha256(key.encode()).hexdigest(); seed = int(h[:8], 16) % 1_000_000
    if conn is None:
        with db.connect() as c:
            c.execute("INSERT INTO api_keys (key_hash, label, requests_per_minute, expires_at, fingerprint) VALUES (%s, %s, %s, %s, %s)", (h, label[:64], rpm, expires, seed))
            c.commit()
    else:
        conn.execute("INSERT INTO api_keys (key_hash, label, requests_per_minute, expires_at, fingerprint) VALUES (%s, %s, %s, %s, %s)", (h, label[:64], rpm, expires, seed))
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


def token() -> str | None:
    t = os.environ.get("TETHERA_TOKEN_ADDRESS", "").strip()
    return t if t.startswith("0x") and len(t) == 42 else None


def burn_message(wallet: str, minute: int | None = None) -> str:
    """The text a wallet signs to claim a key. Bound to the wallet and the minute, so it cannot be replayed later."""
    minute = minute if minute is not None else int(time.time() // 60)
    return f"Tethera access key for {wallet.lower()} at minute {minute}"


def _recover(message: str, signature: str) -> str:
    from eth_account import Account
    from eth_account.messages import encode_defunct
    return Account.recover_message(encode_defunct(text=message), signature=signature)


def _burn_logs(wallet: str) -> list[tuple[str, int, int]]:
    """(tx_hash, log_index, raw amount) of every Transfer of the token from the wallet to the dead address."""
    pad = lambda a: "0x" + a[2:].lower().rjust(64, "0")
    logs = live_rpc(retries=3).call("eth_getLogs", [{"address": token(), "fromBlock": "0x0", "toBlock": "latest",
                                                      "topics": [TRANSFER, pad(wallet), pad(DEAD)]}])
    return [(l["transactionHash"], int(l["logIndex"], 16), int(l["data"], 16)) for l in logs]


def create_burn(wallet: str, signature: str, minute: int, label: str, decimals: int = 18) -> tuple[int, dict]:
    """A key for the wallet that burned the token. The wallet proves itself by signing burn_message(); the burns are read
    from the chain; burns already spent on a key are excluded; the unspent total picks the tier."""
    if not token():
        return 503, {"error": "burn access opens when the token exists"}
    if abs(int(time.time() // 60) - minute) * 60 > MESSAGE_WINDOW_S:
        return 422, {"error": "signed message is too old; sign a fresh one from GET /v1/keys/burn/message"}
    try:
        signer = _recover(burn_message(wallet, minute), signature)
    except Exception as e:
        return 422, {"error": f"signature did not verify: {type(e).__name__}"}
    if signer.lower() != wallet.lower():
        return 422, {"error": "signature is from a different wallet"}
    try:
        burns = _burn_logs(wallet)
    except Exception as e:
        return 502, {"error": f"could not read burns from the chain: {type(e).__name__}"}
    expires = datetime.now(timezone.utc) + timedelta(days=BURN_DAYS)
    with db.connect() as conn:
        spent = {(r[0], r[1]) for r in conn.execute("SELECT tx_hash, log_index FROM key_burns WHERE lower(wallet) = %s", (wallet.lower(),)).fetchall()}
        fresh = [b for b in burns if (b[0], b[1]) not in spent]
        total = sum(b[2] for b in fresh) / 10 ** decimals
        tier = max((t for t in BURN_TIERS if total >= t["tokens"]), key=lambda t: t["tokens"], default=None)
        if tier is None:
            return 422, {"error": f"unspent burns from this wallet total {total:g} tokens; tiers start at {BURN_TIERS[0]['tokens']}", "burns_seen": len(burns)}
        out = _new_key(label or f"burn:{tier['name']}", tier["rpm"], expires, conn=conn)
        h = hashlib.sha256(out["key"].encode()).hexdigest()
        claimed = 0
        for tx, li, amt in fresh:
            claimed += conn.execute("INSERT INTO key_burns (tx_hash, log_index, key_hash, wallet, amount) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                                    (tx, li, h, wallet, amt / 10 ** decimals)).rowcount
        if claimed != len(fresh):                       # a concurrent claim took one of these burns: nothing is kept
            conn.rollback()
            return 409, {"error": "these burns were claimed by another request moments ago; try again"}
        conn.execute("UPDATE api_keys SET wallet = %s, tier = %s WHERE key_hash = %s", (wallet, "burn:" + tier["name"], h))
        conn.commit()
    out.update({"tier": tier["name"], "tokens_burned": total, "days": BURN_DAYS, "transferable": False,
                "licence": "Personal or internal use. Redistribution of outputs is not permitted; outputs are fingerprinted per key and a key found redistributing is revoked without refund."})
    return 201, out


def revoke(key_hash_prefix: str, reason: str) -> int:
    with db.connect() as conn:
        n = conn.execute("UPDATE api_keys SET disabled = true, revoked_reason = %s WHERE key_hash LIKE %s AND NOT disabled",
                         (reason[:200], key_hash_prefix + "%")).rowcount
        conn.commit()
    return n


def fingerprint_of(key: str) -> int | None:
    h = hashlib.sha256(key.encode()).hexdigest()
    with db.connect() as conn:
        row = conn.execute("SELECT fingerprint FROM api_keys WHERE key_hash = %s", (h,)).fetchone()
    return row[0] if row else None


def record_hash(row: dict, prev_hash: str = "") -> str:
    """Canonical hash of a desk record row, chained to the previous row. Covers only fields the fingerprint never
    touches: the minute of `at` (not the seconds), strategy, symbol, action, size, reason, and price to one decimal.
    A reader can recompute it from a fingerprinted feed and check the chain against the published ledger."""
    import json as _json
    at = str(row.get("at", ""))[:16]
    price = round(float(row["price"]), 1) if row.get("price") is not None else None
    body = _json.dumps([prev_hash, at, row.get("strategy"), row.get("symbol"), row.get("action"), round(float(row.get("size", 0)), 4), price, row.get("reason")], sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def fingerprint(rows: list[dict], seed: int | None) -> list[dict]:
    """Per-key marks on a record feed: seconds added to the timestamp (0 to 3, never crossing the minute) and the last
    cent of the price, both deterministic in the key and the row. The record hash covers neither the seconds nor the
    cent, so the chain verifies from a marked feed; order and meaning are unchanged."""
    if seed is None:
        return rows
    out = []
    for r in rows:
        r = dict(r); k = int(hashlib.sha256(f"{seed}:{r.get('id')}".encode()).hexdigest()[:8], 16)
        if isinstance(r.get("at"), str) and len(r["at"]) >= 19:
            try:
                t = datetime.fromisoformat(r["at"]); sec = min(t.second + k % 4, 59)
                r["at"] = t.replace(second=sec).isoformat(timespec="seconds")
            except ValueError:
                pass
        if isinstance(r.get("price"), (int, float)) and r["price"]:
            base = round(r["price"], 1); r["price"] = round(base + (k % 5) * 0.01, 2)   # one of five cents above the tenth the hash covers
        out.append(r)
    return out
