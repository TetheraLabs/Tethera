"""MCP server over the crowding signal, for trading agents that speak the Model Context Protocol.

Runs over stdio, started by the client: PYTHONPATH=src python -m tethera.mcp_server
Wraps the signal module directly; no HTTP in between.
"""
try:
    from mcp.server.mcpserver import MCPServer as FastMCP      # mcp >= 2
except ImportError:                                            # mcp 1.x
    from mcp.server.fastmcp import FastMCP

from . import signal
from .api import _health, _wallet

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
    out = _wallet(address.strip())
    return out if out is not None else {"error": "no fingerprint for this wallet: not labelled automated or registered, or too few swaps in the window"}


@mcp.tool()
def health() -> dict:
    """Whether the signal is current: seconds and blocks since the last swap Tethera has seen.

    lag_seconds above about 60 means the figures from crowding and herds are stale and should not be
    trusted for a live decision.
    """
    return _health()


if __name__ == "__main__":
    mcp.run()
