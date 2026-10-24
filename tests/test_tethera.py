import re
"""Smoke and invariant tests against the running stack on this machine.

They need the API on 127.0.0.1:18790 and the Postgres database; nothing is mocked, because what we want to know is
whether the thing people call actually answers. Run: .venv/bin/python -m pytest -q tests
"""
import os, json, re, tempfile, subprocess, sys
import psycopg, pytest, httpx

API = os.environ.get("TETHERA_API", "http://127.0.0.1:18790")
DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
ROOT = os.path.join(os.path.dirname(__file__), "..")
ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")

@pytest.fixture(scope="session")
def db():
    with psycopg.connect(DB) as conn:
        yield conn

@pytest.fixture(scope="session")
def client(db):
    # the suite makes more calls than the anonymous limit allows in a minute, so it runs on a throwaway key
    import hashlib, secrets
    key = "tk_test_" + secrets.token_urlsafe(16); h = hashlib.sha256(key.encode()).hexdigest()
    db.execute("INSERT INTO api_keys (key_hash, label, requests_per_minute) VALUES (%s, 'pytest-session', 600)", (h,)); db.commit()
    try:
        with httpx.Client(base_url=API, timeout=240, headers={"X-Tethera-Key": key}) as c:
            yield c
    finally:
        db.execute("DELETE FROM api_keys WHERE key_hash = %s", (h,)); db.commit()

@pytest.fixture(scope="session")
def busy_pool(db):
    return db.execute("SELECT pool FROM pool_minutes WHERE minute > now() - interval '1 day' GROUP BY 1 ORDER BY sum(swaps) DESC LIMIT 1").fetchone()[0]


# ---------------- API ----------------

def test_health(client):
    r = client.get("/v1/health"); assert r.status_code == 200
    j = r.json(); assert j["swaps_last_5_min"] > 0 and j["lag_seconds"] is not None and j["latest_swap"].endswith("+00:00")
    assert "jobs" not in json.dumps(j) and not re.search(r'"/[A-Za-z]+/[^"]*"', json.dumps(j))      # no internal paths leak

def test_cors(client):
    r = client.get("/v1/health", headers={"Origin": "https://tetheralabs.com"})
    assert r.headers.get("access-control-allow-origin") == "*"

def test_pool_profile(client, busy_pool):
    r = client.get(f"/v1/pool/{busy_pool}"); assert r.status_code == 200
    j = r.json(); d = j["last_7_days"]
    assert j["token0"]["kind"] and d["swaps"] > 0 and 0 <= d["self_cancelling_share"] <= 1 and isinstance(j["flags"], list)
    assert client.get("/v1/pool/0x0000000000000000000000000000000000000001").status_code == 404

def test_wallet(client, db):
    w = db.execute("SELECT wallet FROM wallet_fingerprints LIMIT 1").fetchone()
    if not w: pytest.skip("no fingerprints yet")
    r = client.get(f"/v1/wallet/{w[0]}"); assert r.status_code == 200 and "signals" in r.json()

def test_wallet_profile(client, db):
    w = db.execute("SELECT tx_from FROM swaps WHERE block_time > now() - interval '1 day' LIMIT 1").fetchone()[0]
    r = client.get(f"/v1/wallet/{w}/profile"); assert r.status_code == 200
    j = r.json(); assert j["wallet"].lower() == w.lower() and "flags" in j and j["last_10_days"]["swaps"] >= 1 and j["reading"]
    assert client.get("/v1/wallet/0x0000000000000000000000000000000000000001/profile").status_code == 404


def test_burn_key_flow(client, db, monkeypatch):
    """The library path in-process (the API runs out of process): a wallet signs the message, burns are read (stubbed),
    one set of burns buys one key, a replay is refused; then the public endpoints: message open flag, record needs a key."""
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from eth_account import Account
    from eth_account.messages import encode_defunct
    from tethera import keys
    acct = Account.create()
    monkeypatch.setattr(keys, "token", lambda: "0x1111111111111111111111111111111111111111")
    tx = "0x" + __import__("secrets").token_hex(32)
    monkeypatch.setattr(keys, "_burn_logs", lambda wallet: [(tx, 7, 150 * 10**18)])
    minute = int(__import__("time").time() // 60); msg = keys.burn_message(acct.address, minute)
    sig = Account.sign_message(encode_defunct(text=msg), acct.key).signature.hex(); sig = sig if sig.startswith("0x") else "0x" + sig
    status, out = keys.create_burn(acct.address, sig, minute, "t"); assert status == 201, out
    assert out["tier"] == "agent" and out["transferable"] is False and out["key"].startswith("tk_")
    assert keys.create_burn(acct.address, sig, minute, "t")[0] == 422                 # same burns cannot buy a second key
    assert keys.create_burn(Account.create().address, sig, minute, "t")[0] == 422     # signature from a different wallet
    assert keys.create_burn(acct.address, sig, minute - 30, "t")[0] == 422           # stale message
    assert keys.fingerprint_of(out["key"]) is not None
    m = client.get(f"/v1/keys/burn/message?wallet={acct.address}").json(); assert "message" in m and m["open"] is False
    assert httpx.get(API + "/v1/desk/record", timeout=30).status_code == 401
    db.execute("DELETE FROM key_burns WHERE tx_hash = %s", (tx,)); db.execute("DELETE FROM api_keys WHERE wallet = %s", (acct.address,)); db.commit()


def test_fingerprint_keeps_the_hash():
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from tethera import keys
    row = {"id": 9, "at": "2026-10-21T15:00:12+00:00", "strategy": "s", "symbol": "QQQ", "action": "enter_long", "size": 1.0, "price": 751.23, "reason": {"k": 1}}
    h = keys.record_hash(row, "prev")
    a, b = keys.fingerprint([row], 11)[0], keys.fingerprint([row], 12)[0]
    assert keys.record_hash(a, "prev") == h == keys.record_hash(b, "prev")
    assert (a["at"], a["price"]) != (b["at"], b["price"]) and keys.fingerprint([row], 11)[0] == a


def test_keys(client, db):
    t = client.get("/v1/keys/tiers").json(); assert t["tiers"] and "open" in t
    r = client.post("/v1/keys", json={"label": "pytest"}); assert r.status_code == 201
    k = r.json()["key"]; assert k.startswith("tk_")
    assert client.get("/v1/keys/tiers", headers={"X-Tethera-Key": k}).headers.get("x-ratelimit-limit") == "120"
    assert client.post("/v1/keys/paid", json={"tx_hash": "0x" + "1" * 64}).status_code in (503, 404, 422)
    db.execute("DELETE FROM api_keys WHERE label = 'pytest'"); db.commit()

# ---------------- data invariants ----------------

def test_one_stable_per_symbol_and_registry(db):
    assert db.execute("SELECT count(*) FROM tokens WHERE kind = 'stable'").fetchone()[0] <= 3
    assert db.execute("SELECT count(*) FROM tokens WHERE kind = 'stable' AND symbol = 'USDG'").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM tokens WHERE official").fetchone()[0] >= 190
    dup = db.execute("SELECT upper(symbol), count(*) FROM tokens WHERE kind IN ('stock', 'stable', 'eth') GROUP BY 1 HAVING count(*) > 1").fetchall()
    assert not dup, f"symbol kept by more than one token: {dup[:3]}"

def test_labels_follow_the_rule(db):
    bad = db.execute("""SELECT count(*) FROM wallet_features WHERE is_automated AND (n_swaps < 5 OR
                        cardinality(signals) - CASE WHEN signals @> ARRAY['same_block_multi_pool','sub_second_gap'] THEN 1 ELSE 0 END < 3)""").fetchone()[0]
    assert bad == 0
    judged, auto = db.execute("SELECT count(*) FILTER (WHERE n_swaps >= 5), count(*) FILTER (WHERE is_automated) FROM wallet_features").fetchone()
    assert 0.005 < auto / judged < 0.15, f"automated share of judged wallets looks wrong: {auto}/{judged}"

def test_self_cancelling_flag_is_consistent(db):
    # every flagged (tx, pool) group has both directions and nets to under 1% on token0
    bad = db.execute("""SELECT count(*) FROM (
        SELECT tx_hash, pool FROM swaps WHERE block_time > now() - interval '1 day' AND self_cancel GROUP BY 1, 2
        HAVING count(DISTINCT direction) < 2 OR abs(sum(amount0)) >= 0.01 * sum(abs(amount0))) x""").fetchone()[0]
    assert bad == 0

def test_pool_minutes_exclude_self_cancelling(db):
    # a pool-minute's swap count never exceeds the non-flagged swaps in that minute
    bad = db.execute("""SELECT count(*) FROM (
        SELECT m.pool, m.minute, m.direction, m.swaps, count(s.*) FILTER (WHERE NOT s.self_cancel) AS real
        FROM pool_minutes m JOIN swaps s ON s.pool = m.pool AND date_trunc('minute', s.block_time) = m.minute AND s.direction = m.direction
        WHERE m.minute > now() - interval '2 hours' AND m.minute < now() - interval '30 minutes'
        GROUP BY 1, 2, 3, 4 HAVING m.swaps > count(s.*) FILTER (WHERE NOT s.self_cancel)) x""").fetchone()[0]
    assert bad == 0

def test_follower_is_current(db):
    lag = db.execute("SELECT extract(epoch FROM now() - max(block_time)) FROM swaps WHERE block_time > now() - interval '1 hour'").fetchone()[0]
    assert lag is not None and lag < 600, f"follower is {lag:.0f}s behind"



# ---------------- desk ----------------

def test_agents_board_and_record_verify(client):
    j = client.get("/v1/desk/agents").json(); assert j["paper"] is True and isinstance(j["agents"], list)
    assert client.get("/v1/desk/agents/no-such-agent").status_code == 404
    sys.path.insert(0, os.path.join(ROOT, "scripts")); sys.path.insert(0, os.path.join(ROOT, "src"))
    import verify_record as V
    from tethera.keys import record_hash, fingerprint
    rows = []; prev = ""
    for i in range(3):
        r = {"id": i + 1, "at": f"2026-10-24T10:0{i}:00+00:00", "strategy": "s", "symbol": "QQQ", "action": "enter_long", "size": 1.0, "price": 700.1 + i, "reason": {"k": i}}
        r["hash"] = record_hash(r, prev); prev = r["hash"]; rows.append(r)
    assert V.verify(rows)[0] == [] and V.verify(fingerprint([dict(r) for r in rows], 5))[0] == []
    rows[1]["size"] = 0.5; assert V.verify(rows)[0]

def test_chain_bars_rule_is_public(db):
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from tethera.instruments import TOKENS
    n = db.execute("SELECT count(DISTINCT symbol) FROM desk_bars_1m").fetchone()[0]
    assert n <= len(TOKENS)
    bad = db.execute("SELECT count(*) FROM desk_bars_1m WHERE low > high OR open > high OR close < low OR volume_usd < 0").fetchone()[0]
    assert bad == 0
