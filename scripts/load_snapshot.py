"""Load data/*.jsonl into Postgres and label swaps by transaction sender.

Idempotent: re-running upserts agents and pools and skips swaps already present.
"""
import json, os, sys, time
import psycopg
from psycopg.types.json import Jsonb

DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)

def rows(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            if line.strip():
                yield json.loads(line)

with psycopg.connect(DB) as conn, conn.cursor() as cur:
    # agents ---------------------------------------------------------------
    n = 0
    for r in rows("data/agents.jsonl"):
        cur.execute("""
            INSERT INTO agents (agent_id, owner, current_owner, agent_wallet, agent_uri, profile, block_number, tx_hash, log_index)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (agent_id) DO UPDATE SET
                current_owner = EXCLUDED.current_owner, agent_wallet = EXCLUDED.agent_wallet,
                agent_uri = EXCLUDED.agent_uri, profile = EXCLUDED.profile
        """, (r["agent_id"], r["owner"], r["current_owner"], r["agent_wallet"], r["agent_uri"],
              Jsonb(r["profile"]) if r["profile"] is not None else None, r["block_number"], r["tx_hash"], r["log_index"]))
        n += 1
    conn.commit(); log(f"agents: {n} rows upserted")

    # pools ----------------------------------------------------------------
    n = 0
    for r in rows("data/pools.jsonl"):
        cur.execute("""
            INSERT INTO pools (pool, protocol, token0, token1, fee, tick_spacing, hooks, created_block)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (pool) DO UPDATE SET protocol = COALESCE(EXCLUDED.protocol, pools.protocol),
                token0 = EXCLUDED.token0, token1 = EXCLUDED.token1
        """, (r["pool"], r.get("protocol"), r["token0"], r["token1"], r.get("fee"), r.get("tick_spacing"), r.get("hooks"), r.get("created_block")))
        n += 1
    conn.commit(); log(f"pools: {n} rows upserted")

    # swaps ----------------------------------------------------------------
    n = 0; batch = []
    def flush():
        global batch
        if batch:
            cur.executemany("""
                INSERT INTO swaps (protocol, pool, block_number, block_time, tx_hash, log_index, event_sender, recipient,
                                   tx_from, amount0, amount1, direction)
                VALUES (%s, %s, %s, to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tx_hash, log_index) DO NOTHING
            """, batch)
            batch = []
    for r in rows("data/swaps.jsonl"):
        if r["tx_from"] is None or r["timestamp"] is None:
            continue
        batch.append((r["protocol"], r["pool"], r["block_number"], r["timestamp"], r["tx_hash"], r["log_index"],
                      r["event_sender"], r["recipient"], r["tx_from"], r["amount0"], r["amount1"], r["direction"]))
        n += 1
        if len(batch) >= 2000:
            flush()
    flush(); conn.commit(); log(f"swaps: {n} rows offered")

    # fill in v2/v3 protocol on pools from the swaps that reference them
    cur.execute("""
        UPDATE pools p SET protocol = s.protocol
        FROM (SELECT DISTINCT pool, protocol FROM swaps) s
        WHERE p.pool = s.pool AND p.protocol IS NULL
    """)
    conn.commit()

    # label ----------------------------------------------------------------
    # A swap is an agent swap when the transaction sender is a registered agent's
    # wallet, or its owner (many agents trade from the owner key). Wallet wins.
    cur.execute("""
        UPDATE swaps s SET is_agent = TRUE, agent_id = a.agent_id
        FROM (
            SELECT DISTINCT ON (addr) addr, agent_id FROM (
                SELECT agent_wallet AS addr, agent_id, 0 AS pri FROM agents WHERE agent_wallet IS NOT NULL
                UNION ALL
                SELECT current_owner, agent_id, 1 FROM agents WHERE current_owner IS NOT NULL
                UNION ALL
                SELECT owner, agent_id, 2 FROM agents
            ) u ORDER BY addr, pri, agent_id
        ) a
        WHERE s.tx_from = a.addr AND s.is_agent IS DISTINCT FROM TRUE
    """)
    labelled = cur.rowcount
    cur.execute("UPDATE swaps SET is_agent = FALSE WHERE is_agent IS NULL")
    conn.commit()
    cur.execute("SELECT count(*), count(*) FILTER (WHERE is_agent), count(DISTINCT tx_from) FILTER (WHERE is_agent) FROM swaps")
    total, agent, agent_wallets = cur.fetchone()
    log(f"labelled: {labelled} new agent swaps; table has {total} swaps, {agent} by agents from {agent_wallets} wallets")
