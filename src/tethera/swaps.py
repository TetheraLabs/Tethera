"""Uniswap v2 / v3 / v4 Swap event reader for Robinhood Chain mainnet.

Produces plain dicts with the transaction's `from` address attached, because the
event's `sender` is almost always a router rather than the wallet that traded.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from eth_abi import decode as abi_decode
from web3 import Web3

from .rpc import Rpc

V2_FACTORY = "0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f"
V3_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
V4_POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"

TOPIC_V2_SWAP = Web3.keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)").hex()
TOPIC_V3_SWAP = Web3.keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()
TOPIC_V4_SWAP = Web3.keccak(text="Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)").hex()
TOPIC_V4_INITIALIZE = Web3.keccak(text="Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)").hex()
SEL_TOKEN0 = Web3.keccak(text="token0()")[:4].hex()
SEL_TOKEN1 = Web3.keccak(text="token1()")[:4].hex()
SEL_FACTORY = Web3.keccak(text="factory()")[:4].hex()


def _h(x: str) -> str:
    return x if x.startswith("0x") else "0x" + x


def _addr(topic: str) -> str:
    return Web3.to_checksum_address("0x" + topic[-40:])


@dataclass
class Swap:
    protocol: str          # 'v2' | 'v3' | 'v4'
    pool: str              # pool/pair address for v2/v3, 32-byte pool id (hex) for v4
    block_number: int
    tx_hash: str
    log_index: int
    event_sender: str      # the `sender` field in the event (usually a router)
    recipient: str | None  # v2 `to`, v3 `recipient`; v4 has none
    amount0: int           # signed: positive = token0 flowed INTO the pool (user sold token0)
    amount1: int
    direction: str         # 'sell0' (token0 in, token1 out) or 'buy0' (token1 in, token0 out)
    tx_from: str | None = None  # filled in by attach_tx_from
    timestamp: int | None = None


def _direction(amount0: int) -> str:
    return "sell0" if amount0 > 0 else "buy0"


def decode_swap(log: dict) -> Swap | None:
    t0 = log["topics"][0].lower()
    data = bytes.fromhex(log["data"][2:])
    base = dict(block_number=int(log["blockNumber"], 16), tx_hash=log["transactionHash"], log_index=int(log["logIndex"], 16))
    if t0 == _h(TOPIC_V2_SWAP).lower():
        a0in, a1in, a0out, a1out = abi_decode(["uint256"] * 4, data)
        amount0, amount1 = a0in - a0out, a1in - a1out
        return Swap("v2", Web3.to_checksum_address(log["address"]), event_sender=_addr(log["topics"][1]),
                    recipient=_addr(log["topics"][2]), amount0=amount0, amount1=amount1, direction=_direction(amount0), **base)
    if t0 == _h(TOPIC_V3_SWAP).lower():
        amount0, amount1, _, _, _ = abi_decode(["int256", "int256", "uint160", "uint128", "int24"], data)
        return Swap("v3", Web3.to_checksum_address(log["address"]), event_sender=_addr(log["topics"][1]),
                    recipient=_addr(log["topics"][2]), amount0=amount0, amount1=amount1, direction=_direction(amount0), **base)
    if t0 == _h(TOPIC_V4_SWAP).lower():
        amount0, amount1, _, _, _, _ = abi_decode(["int128", "int128", "uint160", "uint128", "int24", "uint24"], data)
        # v4 convention is inverted: amount0 < 0 means token0 was taken FROM the user (sold).
        amount0, amount1 = -amount0, -amount1
        return Swap("v4", log["topics"][1], event_sender=_addr(log["topics"][2]),
                    recipient=None, amount0=amount0, amount1=amount1, direction=_direction(amount0), **base)
    return None


def fetch_swaps(rpc: Rpc, from_block: int, to_block: int, chunk: int = 2_000) -> list[Swap]:
    """All v2/v3/v4 swaps in [from_block, to_block]. Uses one topic-OR query per chunk;
    the chunk auto-splits if the node's 10k-log cap is hit."""
    topics = [[_h(TOPIC_V2_SWAP), _h(TOPIC_V3_SWAP), _h(TOPIC_V4_SWAP)]]
    out: list[Swap] = []
    for a in range(from_block, to_block + 1, chunk):
        b = min(a + chunk - 1, to_block)
        for log in rpc.get_logs(a, b, topics=topics):
            s = decode_swap(log)
            if s:
                out.append(s)
    return out


def attach_tx_from(rpc: Rpc, swaps: list[Swap], batch_size: int = 25) -> None:
    """Fill tx_from and timestamp by fetching each distinct block with full transactions."""
    blocks = sorted({s.block_number for s in swaps})
    by_tx: dict[str, str] = {}
    ts: dict[int, int] = {}
    for i in range(0, len(blocks), batch_size):
        chunk = blocks[i:i + batch_size]
        for blk in rpc.batch([("eth_getBlockByNumber", [hex(n), True]) for n in chunk]):
            n = int(blk["number"], 16)
            ts[n] = int(blk["timestamp"], 16)
            for tx in blk["transactions"]:
                by_tx[tx["hash"].lower()] = Web3.to_checksum_address(tx["from"])
    for s in swaps:
        s.tx_from = by_tx.get(s.tx_hash.lower())
        s.timestamp = ts.get(s.block_number)


def fetch_pool_tokens(rpc: Rpc, pools: list[str], batch_size: int = 100) -> dict[str, tuple[str, str]]:
    """token0/token1 for v2 pairs and v3 pools via eth_call."""
    out: dict[str, tuple[str, str]] = {}
    for i in range(0, len(pools), batch_size // 2):
        chunk = pools[i:i + batch_size // 2]
        calls = []
        for p in chunk:
            calls.append(("eth_call", [{"to": p, "data": _h(SEL_TOKEN0)}, "latest"]))
            calls.append(("eth_call", [{"to": p, "data": _h(SEL_TOKEN1)}, "latest"]))
        res = rpc.batch(calls)
        for j, p in enumerate(chunk):
            out[p] = (Web3.to_checksum_address("0x" + res[2 * j][-40:]), Web3.to_checksum_address("0x" + res[2 * j + 1][-40:]))
    return out


def fetch_v4_pools(rpc: Rpc, from_block: int = 1, to_block: int | None = None, chunk: int = 5_000_000) -> dict[str, dict]:
    """All v4 pools from PoolManager Initialize events: pool id -> {currency0, currency1, fee, tick_spacing, hooks}."""
    to_block = to_block or rpc.block_number()
    out: dict[str, dict] = {}
    for a in range(from_block, to_block + 1, chunk):
        b = min(a + chunk - 1, to_block)
        for log in rpc.get_logs(a, b, address=V4_POOL_MANAGER, topics=[_h(TOPIC_V4_INITIALIZE)]):
            fee, tick_spacing, hooks, _, _ = abi_decode(["uint24", "int24", "address", "uint160", "int24"], bytes.fromhex(log["data"][2:]))
            out[log["topics"][1]] = dict(currency0=_addr(log["topics"][2]), currency1=_addr(log["topics"][3]),
                                         fee=fee, tick_spacing=tick_spacing, hooks=Web3.to_checksum_address(hooks),
                                         block_number=int(log["blockNumber"], 16))
    return out


def to_row(s: Swap) -> dict:
    return asdict(s)
