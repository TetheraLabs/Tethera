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
VERDICTS = {"organic", "mixed", "automated", "automated, concentrated", "fresh-wallet driven", "printed", "no activity"}

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

@pytest.fixture(scope="session")
def busy_token(db):
    return db.execute("""SELECT t.token FROM tokens t JOIN pools p ON p.token0 = t.token OR p.token1 = t.token
                         JOIN pool_minutes m ON m.pool = p.pool AND m.minute > now() - interval '1 day'
                         WHERE t.kind = 'other' GROUP BY 1 ORDER BY sum(m.swaps) DESC LIMIT 1""").fetchone()[0]

# ---------------- API ----------------

def test_health(client):
    r = client.get("/v1/health"); assert r.status_code == 200
    j = r.json(); assert j["swaps_last_5_min"] > 0 and j["lag_seconds"] is not None and j["latest_swap"].endswith("+00:00")
    assert "jobs" not in json.dumps(j) and not re.search(r'"/[A-Za-z]+/[^"]*"', json.dumps(j))      # no internal paths leak

def test_cors(client):
    r = client.get("/v1/health", headers={"Origin": "https://tetheralabs.com"})
    assert r.headers.get("access-control-allow-origin") == "*"

def test_crowding(client, busy_pool):
    r = client.get("/v1/crowding", params={"pool": busy_pool, "direction": "sell0"}); assert r.status_code == 200
    j = r.json(); assert j["verdict"] in {"clean", "busy", "herd"} and j["auto_wallets_same"] >= 0
    assert client.get("/v1/crowding", params={"pool": busy_pool, "direction": "sideways"}).status_code == 422

def test_herds(client):
    r = client.get("/v1/herds"); assert r.status_code == 200 and isinstance(r.json()["herds"], list)

def test_pool_profile(client, busy_pool):
    r = client.get(f"/v1/pool/{busy_pool}"); assert r.status_code == 200
    j = r.json(); d = j["last_7_days"]
    assert j["token0"]["kind"] and d["swaps"] > 0 and 0 <= d["self_cancelling_share"] <= 1 and isinstance(j["flags"], list)
    assert client.get("/v1/pool/0x0000000000000000000000000000000000000001").status_code == 404

def test_token_audit(client, busy_token):
    r = client.get(f"/v1/token/{busy_token}"); assert r.status_code == 200
    j = r.json(); assert j["verdict"] in VERDICTS and j["swaps"] > 0 and j["pools"] and j["automated_swap_share"] is not None
    r2 = client.get(f"/v1/token/{busy_token}"); assert r2.json()["cached"] is True
    assert client.get("/v1/token/0x0000000000000000000000000000000000000001").status_code == 404
    assert client.get("/v1/token/notanaddress").status_code == 422

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


def test_series_and_flock(client):
    s = client.get("/v1/series", params={"hours": 24}).json(); assert 20 <= len(s["points"]) <= 25
    p = s["points"][-1]; assert set(("hour", "swaps", "automated_share", "herd_minutes")) <= set(p)
    f = client.get("/v1/flock").json(); assert "pools" in f and f["window_seconds"] == 60

def test_badge(client, busy_token):
    r = client.get(f"/v1/token/{busy_token}/badge.svg"); assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert "tethera" in r.text and "<svg" in r.text

def test_alerts_and_stream(client):
    j = client.get("/v1/alerts").json(); assert "alerts" in j and j["last_id"] >= 0
    with client.stream("GET", "/v1/alerts/stream", timeout=10) as s:
        first = next(s.iter_lines()); assert first.startswith("retry:")

def test_keys(client, db):
    t = client.get("/v1/keys/tiers").json(); assert t["tiers"] and "open" in t
    r = client.post("/v1/keys", json={"label": "pytest"}); assert r.status_code == 201
    k = r.json()["key"]; assert k.startswith("tk_")
    assert client.get("/v1/herds", headers={"X-Tethera-Key": k}).headers.get("x-ratelimit-limit") == "120"
    assert client.post("/v1/keys/paid", json={"tx_hash": "0x" + "1" * 64}).status_code in (503, 404, 422)
    db.execute("DELETE FROM api_keys WHERE label = 'pytest'"); db.commit()

def test_real_list(client):
    anon = httpx.get(f"{API}/v1/tokens", timeout=240).json()          # no key: gated
    assert anon["gated"] is True and len(anon["rows"]) <= 10 and anon["total"] >= 10
    full = client.get("/v1/tokens", params={"hours": 24}).json()      # keyed: full
    assert full["gated"] is False and len(full["rows"]) > 10
    r = full["rows"][0]; assert r["organic_swaps"] > 0 and 0 <= (r["automated_swap_share"] or 0) <= 1 and r["token"].startswith("0x")
    stock = client.get("/v1/tokens", params={"hours": 24, "kind": "stock"}).json(); assert all(x["kind"] == "stock" for x in stock["rows"])
    assert client.get("/v1/tokens", params={"hours": 48}).status_code == 422
    csv = client.get("/v1/tokens.csv"); assert csv.status_code == 200 and csv.text.startswith("rank,symbol,")
    assert httpx.get(f"{API}/v1/tokens.csv", timeout=60).status_code == 401

def test_calm_history_exposure(client, busy_pool, busy_token, db):
    c = client.get("/v1/calm", params={"pool": busy_pool, "direction": "buy0", "max_wait": 0}).json()
    assert c["calm"] in (True, False) and c["verdict"] in {"clean", "busy", "herd"}
    client.get(f"/v1/token/{busy_token}")                                   # makes sure one audit row exists
    h = client.get(f"/v1/token/{busy_token}/history").json(); assert h["points"] and h["points"][-1]["verdict"]
    w = db.execute("SELECT tx_from FROM swaps WHERE block_time > now() - interval '1 hour' AND NOT self_cancel LIMIT 1").fetchone()[0]
    e = client.get(f"/v1/wallet/{w}/exposure").json(); assert e["swaps"] > 0 and 0 <= e["share_with_herd"] <= 1

def test_mcp_over_http(client):
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    assert client.post("/mcp", json=init, headers=h).status_code == 200
    tools = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=h).json()["result"]["tools"]
    assert {"crowding", "herds", "pool", "wallet", "token", "health", "wait_for_calm", "real_list", "wallet_profile"} <= {t["name"] for t in tools}
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "health", "arguments": {}}}, headers=h).json()
    assert "swaps_last_5_min" in r["result"]["content"][0]["text"]

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

# ---------------- publisher ----------------

def test_publisher_round_trip(tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("pi", os.path.join(ROOT, "scripts", "publish_issue.py")); pi = importlib.util.module_from_spec(spec); spec.loader.exec_module(pi)
    out = tmp_path / "out"; out.mkdir(); pi.OUT = str(out)
    src = tmp_path / "issue.md"; src.write_text("---\ndate: 2030-01-01\ntitle: Test\nsummary: s\nkind: weekly\n---\n# H\n\nBody **bold**.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    (out / "notes.md").write_text("not an issue")        # must be ignored by the index
    pi.main(str(src))
    idx = json.loads((out / "issues.json").read_text())
    assert len(idx["issues"]) == 1 and idx["issues"][0]["title"] == "Test"
    assert (out / "2030-01-01.html").exists() and (out / "2030-01-01.csv").exists() and (out / "2030-01-01.pdf").stat().st_size > 1000
    assert "<b>bold</b>" in (out / "2030-01-01.html").read_text()

# ---------------- MCP ----------------

def test_mcp_tools_answer():
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import importlib; m = importlib.import_module("tethera.mcp_server")
    h = m.health(); assert "swaps_last_5_min" in h
    assert "herds" in m.herds()
    assert m.pool("0x0000000000000000000000000000000000000001") == {"error": "unknown pool"}
    assert m.token("0x0000000000000000000000000000000000000001") == {"error": "unknown token"}
