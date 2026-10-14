"""ERC-8004 Identity Registry reader for Robinhood Chain mainnet.

Produces plain dicts; the database layer decides where they go.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, asdict

from eth_abi import decode as abi_decode
from web3 import Web3

from .rpc import Rpc

IDENTITY_REGISTRY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
FIRST_BLOCK = 34_617_892  # first registration, 2026-08-12

TOPIC_REGISTERED = Web3.keccak(text="Registered(uint256,string,address)").hex()
TOPIC_URI_UPDATED = Web3.keccak(text="URIUpdated(uint256,string,address)").hex()
SEL_GET_AGENT_WALLET = Web3.keccak(text="getAgentWallet(uint256)")[:4].hex()
SEL_TOKEN_URI = Web3.keccak(text="tokenURI(uint256)")[:4].hex()
SEL_OWNER_OF = Web3.keccak(text="ownerOf(uint256)")[:4].hex()


def _h(x: str) -> str:
    return x if x.startswith("0x") else "0x" + x


@dataclass
class Registration:
    agent_id: int
    owner: str
    agent_uri: str
    block_number: int
    tx_hash: str
    log_index: int


def fetch_registrations(rpc: Rpc, from_block: int = FIRST_BLOCK, to_block: int | None = None,
                        chunk: int = 2_000_000) -> list[Registration]:
    to_block = to_block or rpc.block_number()
    out: list[Registration] = []
    for a in range(from_block, to_block + 1, chunk):
        b = min(a + chunk - 1, to_block)
        for log in rpc.get_logs(a, b, address=IDENTITY_REGISTRY, topics=[_h(TOPIC_REGISTERED)]):
            (uri,) = abi_decode(["string"], bytes.fromhex(log["data"][2:]))
            out.append(Registration(
                agent_id=int(log["topics"][1], 16),
                owner=Web3.to_checksum_address("0x" + log["topics"][2][-40:]),
                agent_uri=uri,
                block_number=int(log["blockNumber"], 16),
                tx_hash=log["transactionHash"],
                log_index=int(log["logIndex"], 16),
            ))
    return out


def _uint_arg(n: int) -> str:
    return n.to_bytes(32, "big").hex()


def fetch_agent_wallets(rpc: Rpc, agent_ids: list[int], batch_size: int = 50) -> dict[int, str | None]:
    """getAgentWallet(agentId) for each id. Zero address -> None."""
    wallets: dict[int, str | None] = {}
    for i in range(0, len(agent_ids), batch_size):
        ids = agent_ids[i:i + batch_size]
        calls = [("eth_call", [{"to": IDENTITY_REGISTRY, "data": _h(SEL_GET_AGENT_WALLET) + _uint_arg(a)}, "latest"]) for a in ids]
        for agent_id, res in zip(ids, rpc.batch(calls)):
            addr = "0x" + res[-40:] if res and len(res) >= 42 else None
            wallets[agent_id] = None if not addr or int(addr, 16) == 0 else Web3.to_checksum_address(addr)
    return wallets


def fetch_current_owners(rpc: Rpc, agent_ids: list[int], batch_size: int = 50) -> dict[int, str | None]:
    """ownerOf(agentId) now, since the NFT may have been transferred since registration."""
    owners: dict[int, str | None] = {}
    for i in range(0, len(agent_ids), batch_size):
        ids = agent_ids[i:i + batch_size]
        calls = [("eth_call", [{"to": IDENTITY_REGISTRY, "data": _h(SEL_OWNER_OF) + _uint_arg(a)}, "latest"]) for a in ids]
        try:
            results = rpc.batch(calls)
        except Exception:
            results = [None] * len(ids)  # burned tokens revert; treat as unknown
        for agent_id, res in zip(ids, results):
            owners[agent_id] = Web3.to_checksum_address("0x" + res[-40:]) if res and len(res) >= 42 else None
    return owners


def decode_agent_uri(uri: str) -> dict | None:
    """Return the registration JSON if the URI is an inline data: URI; None if it must be fetched over HTTP."""
    if not uri.startswith("data:"):
        return None
    header, _, payload = uri.partition(",")
    try:
        raw = base64.b64decode(payload) if ";base64" in header else payload.encode()
        return json.loads(raw)
    except Exception:
        return {"_undecodable": uri[:500]}


def to_row(r: Registration) -> dict:
    return asdict(r)
