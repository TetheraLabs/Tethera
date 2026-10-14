"""The crowding signal: how many automated wallets are moving the same way in a pool right now,
how one-sided it is, how many pools are moving together, and how unusual that is for this pool.

Reads raw swaps for the live window and pool_minutes for the pool's history.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone

from . import db

WINDOW_S = 60


@dataclass
class Crowding:
    pool: str
    direction: str
    as_of: str
    window_seconds: int
    auto_wallets_same: int        # automated wallets trading this direction in the window
    auto_wallets_opposite: int
    all_wallets_same: int
    auto_swaps_same: int
    one_sidedness: float          # same / (same + opposite) among automated wallets; 0.5 = balanced
    pools_moving_same_way: int    # pools with >=10 automated wallets on this side in the window (chain-wide)
    percentile: float | None      # where auto_wallets_same sits in this pool's per-minute history for this direction
    verdict: str                  # clean | busy | herd
    explanation: str


def _verdict(same: int, opp: int, pct: float | None) -> tuple[str, str]:
    if same < 5:
        return "clean", "fewer than 5 automated wallets on this side in the last minute"
    onesided = opp <= 0.15 * same
    if same >= 15 and onesided:
        return "herd", f"{same} automated wallets this way vs {opp} the other way; one-sided"
    if same >= 15:
        return "busy", f"{same} automated wallets this way but {opp} the other way; two-sided flow"
    if pct is not None and pct >= 0.95:
        return "busy", f"{same} automated wallets this way, above the 95th percentile for this pool"
    return "clean", f"{same} automated wallets this way; normal for this pool"


def crowding(pool: str, direction: str, as_of: datetime | None = None, conn=None) -> Crowding:
    own = conn is None
    conn = conn or db.connect()
    try:
        cur = conn.cursor()
        if as_of is None:
            cur.execute("SELECT max(block_time) FROM swaps WHERE block_time > now() - interval '10 minutes'")
            as_of = cur.fetchone()[0] or datetime.now(timezone.utc)
        start = as_of - timedelta(seconds=WINDOW_S)
        opp = "buy0" if direction == "sell0" else "sell0"
        cur.execute("""
            SELECT count(DISTINCT tx_from) FILTER (WHERE direction = %s AND is_automated),
                   count(DISTINCT tx_from) FILTER (WHERE direction = %s AND is_automated),
                   count(DISTINCT tx_from) FILTER (WHERE direction = %s),
                   count(*) FILTER (WHERE direction = %s AND is_automated)
            FROM swaps WHERE pool = %s AND block_time > %s AND block_time <= %s
        """, (direction, opp, direction, direction, pool, start, as_of))
        same, opposite, all_same, swaps_same = cur.fetchone()
        cur.execute("""
            SELECT count(*) FROM (
                SELECT pool FROM swaps WHERE direction = %s AND is_automated AND block_time > %s AND block_time <= %s
                GROUP BY pool HAVING count(DISTINCT tx_from) >= 10) p
        """, (direction, start, as_of))
        pools_same = cur.fetchone()[0]
        cur.execute("""
            SELECT count(*) FILTER (WHERE auto_wallets < %s)::float / nullif(count(*), 0)
            FROM pool_minutes WHERE pool = %s AND direction = %s AND minute < date_trunc('minute', %s::timestamptz)
        """, (same, pool, direction, as_of))
        pct = cur.fetchone()[0]
        onesided = same / (same + opposite) if (same + opposite) else 0.5
        verdict, why = _verdict(same, opposite, pct)
        return Crowding(pool, direction, as_of.isoformat(), WINDOW_S, same, opposite, all_same, swaps_same,
                        round(onesided, 3), pools_same, None if pct is None else round(pct, 3), verdict, why)
    finally:
        if own:
            conn.close()


def current_herds(min_wallets: int = 15, conn=None) -> list[dict]:
    """Pools with a one-sided automated crowd in the last minute, biggest first."""
    own = conn is None
    conn = conn or db.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT max(block_time) FROM swaps WHERE block_time > now() - interval '10 minutes'")
        as_of = cur.fetchone()[0] or datetime.now(timezone.utc)
        cur.execute("""
            WITH w AS (
              SELECT pool,
                     count(DISTINCT tx_from) FILTER (WHERE direction='sell0' AND is_automated) AS s,
                     count(DISTINCT tx_from) FILTER (WHERE direction='buy0'  AND is_automated) AS b
              FROM swaps WHERE block_time > %s - interval '60 seconds' AND block_time <= %s GROUP BY pool)
            SELECT pool, CASE WHEN s >= b THEN 'sell0' ELSE 'buy0' END, greatest(s,b), least(s,b)
            FROM w WHERE greatest(s,b) >= %s AND least(s,b) <= 0.15 * greatest(s,b)
            ORDER BY greatest(s,b) DESC
        """, (as_of, as_of, min_wallets))
        return [{"pool": p, "direction": d, "auto_wallets_same": s, "auto_wallets_opposite": o, "as_of": as_of.isoformat()}
                for p, d, s, o in cur.fetchall()]
    finally:
        if own:
            conn.close()


def to_dict(c: Crowding) -> dict:
    return asdict(c)
