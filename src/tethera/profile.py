"""Reputation profile of any wallet address: how old it is, how much it has done, whether it behaves like
software, who it trades with, and whether it shows up early in new pools. Built from our own tables
(swaps, wallet_first_seen, wallet_features, wallet_fingerprints, wallet_pairs, wallet_clusters,
wallet_nonce) plus one cached on-chain transaction count. Nothing here is an identity claim: it says
what the address has done on this chain, not who holds it.
"""
from datetime import datetime, timezone
from web3 import Web3
from . import db
from .rpc import live_rpc

LAUNCH_BLOCKS = 36_000          # about an hour at this chain's block rate: "early" in a pool's life
NONCE_MAX_AGE = "7 days"
FLAG_WORDS = {
    "contract": "the address has code: a contract or a delegated smart account, not a plain wallet",
    "registered_agent": "registered as an ERC-8004 agent",
    "automated": "labelled automated by its swap pattern over the last week",
    "volume_bot": "labelled a volume bot: trades that mostly cancel themselves",
    "fresh": "first seen within the last 7 days with at most 3 transactions on the chain",
    "early_in_launches": "entered five or more pools within an hour of their creation in the last ten days",
    "in_a_family": "keeps trading the same pool, minute and side as other wallets",
    "self_cancelling": "at least 30% of its swaps are self-cancelling",
}


def _utc(t):
    return t.astimezone(timezone.utc).isoformat(timespec="seconds") if t else None


def _nonce(conn, wallet):
    row = conn.execute(f"SELECT nonce, fetched_at FROM wallet_nonce WHERE wallet = %s AND fetched_at > now() - interval '{NONCE_MAX_AGE}'", (wallet,)).fetchone()
    if row:
        return row[0]
    try:
        n = int(live_rpc(retries=2).call("eth_getTransactionCount", [wallet, "latest"]), 16)
    except Exception:
        return None
    conn.execute("INSERT INTO wallet_nonce (wallet, nonce) VALUES (%s, %s) ON CONFLICT (wallet) DO UPDATE SET nonce = EXCLUDED.nonce, fetched_at = now()", (wallet, n))
    conn.commit()
    return n


def wallet_profile(address: str) -> dict | None:
    """None when the address has never swapped in anything we hold."""
    w = Web3.to_checksum_address(address)
    with db.connect() as conn:
        conn.execute("SET statement_timeout = '30s'")
        hot = conn.execute("""SELECT count(*), count(DISTINCT pool), min(block_time), max(block_time),
                                     count(*) FILTER (WHERE self_cancel), count(*) FILTER (WHERE is_automated)
                              FROM swaps WHERE tx_from = %s""", (w,)).fetchone()
        ever = conn.execute("SELECT first_time, last_block, n_swaps FROM wallet_first_seen WHERE wallet = %s", (w,)).fetchone()
        feat = conn.execute("""SELECT is_automated, is_volume_bot, is_registered_agent, signals, lifetime_tx_count, is_contract, n_swaps, n_pools, window_end
                               FROM wallet_features WHERE wallet = %s""", (w,)).fetchone()
        fp = conn.execute("""SELECT a.label, a.description, f.family_id, f.family_size FROM wallet_fingerprints f
                             LEFT JOIN fingerprint_archetypes a USING (archetype_id) WHERE f.wallet = %s""", (w,)).fetchone()
        cl = conn.execute("SELECT cluster_id, size FROM wallet_clusters WHERE wallet = %s AND window_end = (SELECT max(window_end) FROM wallet_clusters)", (w,)).fetchone()
        partners = conn.execute("""SELECT CASE WHEN wallet_a = %s THEN wallet_b ELSE wallet_a END, together, round(jaccard::numeric, 3)
                                   FROM wallet_pairs WHERE (wallet_a = %s OR wallet_b = %s) AND window_end = (SELECT max(window_end) FROM wallet_pairs)
                                   ORDER BY jaccard DESC LIMIT 5""", (w, w, w)).fetchall()
        early = conn.execute("""WITH my AS (SELECT pool, min(block_number) AS b FROM swaps WHERE tx_from = %s GROUP BY 1)
                                SELECT count(*) FILTER (WHERE my.b <= p.created_block + %s), count(*) FILTER (WHERE p.created_block IS NOT NULL)
                                FROM my JOIN pools p USING (pool)""", (w, LAUNCH_BLOCKS)).fetchone()
        n_hot, pools_hot, first_hot, last_hot, sc_hot, auto_hot = hot
        if not n_hot and not ever:
            return None
        nonce = _nonce(conn, w)

    now = datetime.now(timezone.utc)
    first_seen = ever[0] if ever else first_hot
    exact = bool(ever)
    age_days = round((now - first_seen).total_seconds() / 86400, 1) if first_seen else None
    tx_count = nonce if nonce is not None else (feat[4] if feat else None)
    flags = []
    if feat and feat[5]: flags.append("contract")
    if feat and feat[2]: flags.append("registered_agent")
    if feat and feat[0]: flags.append("automated")
    if feat and feat[1]: flags.append("volume_bot")
    if age_days is not None and age_days <= 7 and tx_count is not None and tx_count <= 3: flags.append("fresh")
    if early[0] >= 5: flags.append("early_in_launches")
    if (fp and fp[3] and fp[3] >= 3) or (cl and cl[1] >= 3): flags.append("in_a_family")
    if n_hot and sc_hot / n_hot >= 0.3: flags.append("self_cancelling")

    if not flags:
        reading = "Nothing in its pattern stands out: trades by hand as far as the swap record shows, not new, not part of a group."
    else:
        reading = "; ".join(FLAG_WORDS[f] for f in flags).capitalize() + "."
    return {
        "wallet": w,
        "first_seen": _utc(first_seen), "first_seen_is_exact": exact,
        "first_seen_note": "exact since 1 July 2026 from the archive" if exact else "earliest swap in the live table only (about ten days); the archive-backed date arrives when the wallet-age table is built",
        "age_days": age_days,
        "transactions_on_chain": tx_count,
        "swaps_all_time_since_july": ever[2] if ever else None,
        "last_10_days": {"swaps": n_hot, "pools": pools_hot, "automated_swaps": auto_hot, "self_cancelling_swaps": sc_hot,
                         "last_swap": _utc(last_hot), "pools_entered_within_an_hour_of_creation": early[0], "pools_with_known_creation": early[1]},
        "labels": {"is_automated": feat[0] if feat else False, "is_volume_bot": feat[1] if feat else False,
                   "is_registered_agent": feat[2] if feat else False, "is_contract": feat[5] if feat else None,
                   "signals": feat[3] if feat else [], "labelled_at": _utc(feat[8]) if feat else None},
        "behaviour": {"archetype": fp[0] if fp else None, "archetype_description": fp[1] if fp else None,
                      "family_size": fp[3] if fp else None, "cluster_size": cl[1] if cl else None,
                      "closest_partners": [{"wallet": p[0], "minutes_together": p[1], "jaccard": float(p[2])} for p in partners]},
        "flags": flags, "reading": reading,
        "note": "Behaviour on this chain's Uniswap pools since 1 July 2026, not identity. Labels are recomputed every 10 minutes, families nightly.",
    }
