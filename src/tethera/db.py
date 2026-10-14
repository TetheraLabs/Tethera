"""Thin Postgres helpers shared by the follower, the labeller and the API."""
from __future__ import annotations

import os
import psycopg

DB_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")

SWAP_INSERT = """
INSERT INTO swaps (protocol, pool, block_number, block_time, tx_hash, log_index, event_sender, recipient,
                   tx_from, amount0, amount1, direction, is_automated)
VALUES (%s,%s,%s,to_timestamp(%s),%s,%s,%s,%s,%s,%s,%s,%s,
        (SELECT is_automated FROM wallet_features WHERE wallet = %s))
ON CONFLICT (tx_hash, log_index) DO NOTHING
"""


def connect(autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(DB_URL, autocommit=autocommit)


def insert_swaps(cur, swaps) -> int:
    rows = [(s.protocol, s.pool, s.block_number, s.timestamp, s.tx_hash, s.log_index, s.event_sender, s.recipient,
             s.tx_from, s.amount0, s.amount1, s.direction, s.tx_from) for s in swaps if s.tx_from and s.timestamp]
    if rows:
        cur.executemany(SWAP_INSERT, rows)
    return len(rows)
