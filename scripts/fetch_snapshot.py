"""Fetch a snapshot from the chain into data/ as JSON lines, independent of the database.

  1. every ERC-8004 registration, with agent wallet, current owner and decoded profile
  2. the last N hours of Uniswap v2/v3/v4 swaps, each with its transaction sender
  3. every Uniswap v4 pool (from PoolManager Initialize events)

Usage: python scripts/fetch_snapshot.py [hours]
"""
import json, sys, time, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from tethera.rpc import Rpc
from tethera import registry, swaps as sw

HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
BLOCKS_PER_HOUR = 36_000
rpc = Rpc()
latest = rpc.block_number()
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)

# 1. registry ----------------------------------------------------------------
t = time.time()
regs = registry.fetch_registrations(rpc, to_block=latest)
ids = [r.agent_id for r in regs]
wallets = registry.fetch_agent_wallets(rpc, ids)
owners = registry.fetch_current_owners(rpc, ids)
with open("data/agents.jsonl", "w") as f:
    for r in regs:
        row = registry.to_row(r)
        row["agent_wallet"] = wallets.get(r.agent_id)
        row["current_owner"] = owners.get(r.agent_id)
        row["profile"] = registry.decode_agent_uri(r.agent_uri)
        f.write(json.dumps(row) + "\n")
log(f"registry: {len(regs)} registrations, {sum(1 for v in wallets.values() if v)} with wallets, {time.time()-t:.0f}s")

# 2. swaps -------------------------------------------------------------------
start = latest - int(HOURS * BLOCKS_PER_HOUR)
CH = 2_000
n = 0; t = time.time()
with open("data/swaps.jsonl", "w") as f:
    for a in range(start, latest + 1, CH):
        b = min(a + CH - 1, latest)
        chunk = sw.fetch_swaps(rpc, a, b)
        sw.attach_tx_from(rpc, chunk)
        for s in chunk:
            f.write(json.dumps(sw.to_row(s)) + "\n")
        n += len(chunk)
        done = (b - start + 1) / (latest - start + 1)
        log(f"swaps: blocks {a}-{b} -> {len(chunk)} (total {n}), {done:.0%} done, {time.time()-t:.0f}s elapsed")
log(f"swaps: {n} total from block {start} to {latest}")

# 3. pools -------------------------------------------------------------------
t = time.time()
with open("data/swaps.jsonl") as f:
    seen = {json.loads(l)["pool"] for l in f}
v2v3 = [p for p in seen if len(p) == 42]
tokens = sw.fetch_pool_tokens(rpc, v2v3)
v4 = sw.fetch_v4_pools(rpc, to_block=latest)
with open("data/pools.jsonl", "w") as f:
    for p, (t0, t1) in tokens.items():
        f.write(json.dumps({"pool": p, "protocol": None, "token0": t0, "token1": t1}) + "\n")
    for pid, d in v4.items():
        f.write(json.dumps({"pool": pid, "protocol": "v4", "token0": d["currency0"], "token1": d["currency1"],
                            "fee": d["fee"], "tick_spacing": d["tick_spacing"], "hooks": d["hooks"], "created_block": d["block_number"]}) + "\n")
log(f"pools: {len(tokens)} v2/v3 pools with swaps, {len(v4)} v4 pools total, {time.time()-t:.0f}s")
log("snapshot complete")
