"""MCP server over the crowding signal, for trading agents that speak the Model Context Protocol.

Two ways in:
  - over HTTP, hosted: https://api.tetheralabs.com/mcp (streamable HTTP; add the URL to your client, no key needed)
  - over stdio, local: PYTHONPATH=src python -m tethera.mcp_server
Both wrap the signal module directly; no HTTP API in between.
"""
try:
    from mcp.server.mcpserver import MCPServer as FastMCP      # mcp >= 2
except ImportError:                                            # mcp 1.x
    from mcp.server.fastmcp import FastMCP

from . import signal
# the API module imports this one to mount it at /mcp, so its helpers are imported inside the tools, not at load

mcp = FastMCP("tethera", instructions=(
    "Tethera measures herding among automated traders on Robinhood Chain. Before placing a swap, "
    "call crowding for the pool and side you intend to trade. A 'herd' verdict means many automated "
    "wallets are already pushing the same way, so the pool price is moving against you and the trade "
    "is likely to fill worse than quoted; 'busy' means the pool is active but two-sided; 'clean' means "
    "nothing unusual. Counts cover the last 60 seconds."))


@mcp.tool()
def crowding(pool: str, direction: str, size: float | None = None) -> dict:
    """How crowded one side of one pool is right now.

    pool: the pool address (v2/v3) or 32-byte pool id (v4). direction: 'sell0' if you are selling
    token0 into the pool, 'buy0' if you are buying token0. size: your intended size in token0 units
    (optional, not yet used). Returns auto_wallets_same (automated wallets already trading your way
    in the last 60 s), auto_wallets_opposite, one_sidedness (share of automated wallets on your side,
    0.5 = balanced, 1.0 = everyone the same way), pools_moving_same_way (pools chain-wide with a crowd
    on this side, a sign of a shared signal), percentile (where this minute sits in the pool's own
    history, 0.95 = busier than 95% of past minutes) and a verdict: clean, busy or herd.
    """
    if direction not in ("sell0", "buy0"):
        raise ValueError("direction must be 'sell0' or 'buy0'")
    out = signal.to_dict(signal.crowding(pool, direction))
    out["size"] = size
    return out


@mcp.tool()
def herds(min_wallets: int = 15) -> dict:
    """Every pool with a one-sided automated crowd in the last minute, biggest first.

    A pool is listed when at least min_wallets automated wallets traded it the same way in the last
    60 seconds and the other side had at most 15% as many. auto_wallets_same is the crowd size,
    auto_wallets_opposite the wallets going the other way. Use it to see where herds are forming
    before choosing a pool, or to avoid pools already in a stampede.
    """
    return {"herds": signal.current_herds(max(5, min(500, int(min_wallets))))}


@mcp.tool()
def pool(pool: str) -> dict:
    """Trust profile of one pool over the last 7 days: organic volume, self-cancelling (printed) volume and share,
    automated share, herd minutes, token kinds (official Robinhood Stock Token or copycat), fee tier and flags.
    `pool` is the v2/v3 address or the 32-byte v4 pool id. Returns {"error": ...} for an unknown pool."""
    from .api import _pool
    out = _pool(pool.strip())
    return out if out is not None else {"error": "unknown pool"}


@mcp.tool()
def token(address: str, days: float = 7) -> dict:
    """Launch audit of one token over the last `days` (1 to 30): how much of its trading was real and who was there.

    Returns swaps, wallets, automated_swap_share, self-cancelling (printed) share, fresh_wallet_share (wallets whose
    first swap in our data was in this token), born_together (largest batch of fresh wallets first seen in one
    ten-minute bucket), top10_share (ten busiest wallets' share of swaps), one_swap_share, the pools, and a verdict:
    organic, mixed, automated, automated-concentrated, fresh-wallet driven or printed. Cached 30 minutes.
    Use it before trading or listing a token, or to answer "is this token's volume real".
    Returns {"error": ...} for an unknown token."""
    from .audit import audit_token
    out = audit_token(address.strip(), days=max(1.0, min(30.0, float(days))))
    return out if out is not None else {"error": "unknown token"}


@mcp.tool()
def wallet(address: str) -> dict:
    """Behavioural fingerprint of one wallet: what it behaves like, not what it is.

    Covers wallets labelled automated or registered as ERC-8004 agents with enough swaps in the last
    week. Returns is_automated and the signals behind it, the archetype (a cluster of behaviour with a
    plain-words label and description: cadence, pools, sizing, hours), the family (wallets that keep
    trading the same pool, minute and side together) with its size, the three nearest registered
    agents in feature space, and the key numbers (median_gap, gap_cv, period_s, fast_share, n_pools,
    stock_share, round_amount_frac, reactive_share, round_trips_per_100, net_flow_frac, ...). Use it
    to judge whether a counterparty or a crowd member is a bot, how it trades and who it resembles.
    Returns {"error": ...} when the wallet has no fingerprint.
    """
    from .api import _wallet
    out = _wallet(address.strip())
    return out if out is not None else {"error": "no fingerprint for this wallet: not labelled automated or registered, or too few swaps in the window"}


@mcp.tool()
def wallet_profile(address: str) -> dict:
    """Reputation profile of any wallet address, not only labelled ones: first seen on the chain (exact since July 2026),
    age in days, transaction count, swaps and pools in the last ten days, the automated / volume-bot / registered-agent
    labels, family and cluster size with closest partners, how many new pools it entered within an hour of creation,
    and plain-words flags (contract, registered_agent, automated, volume_bot, fresh, early_in_launches, in_a_family,
    self_cancelling) with a one-sentence reading. Use it to judge a counterparty, an airdrop claimant, or an early
    buyer of a launch. Returns {"error": ...} when the address has never swapped here."""
    from . import profile
    out = profile.wallet_profile(address.strip())
    return out if out is not None else {"error": "no swaps from this address in anything we hold"}


@mcp.tool()
def wait_for_calm(pool: str, direction: str, max_wait: int = 60) -> dict:
    """Block until your side of the pool is not a herd, or until max_wait seconds (0 to 120) pass. Returns the crowding
    reading with `calm` true or false and `waited_seconds`. Use it right before placing a swap when you have no urgency:
    herds on this chain usually clear within a minute, and trading after one clears avoids filling into the crowd."""
    from .api import wait_for_calm as w
    return w(pool, direction, max(0, min(120, int(max_wait))))


@mcp.tool()
def real_list(hours: int = 24) -> dict:
    """Tokens on Robinhood Chain ranked by real activity over the last 24 hours (or 168 for a week): organic swaps with
    self-cancelling trades removed, wallets, automated share, printed share, USD volume where stablecoin-quoted, whether
    the token is new, and the audit verdict. The top ten; the full list is at api.tetheralabs.com/v1/tokens with a key."""
    from . import rank
    return rank.get(168 if int(hours) >= 168 else 24, keyed=False)


@mcp.tool()
def health() -> dict:
    """Whether the signal is current: seconds and blocks since the last swap Tethera has seen.

    lag_seconds above about 60 means the figures from crowding and herds are stale and should not be
    trusted for a live decision.
    """
    from .api import _health
    return _health()


if __name__ == "__main__":
    mcp.run()
