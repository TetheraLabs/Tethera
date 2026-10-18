# Tethera contracts

Two contracts, built with Foundry against Uniswap v4 core.

- `src/AutomatedSet.sol`: the public on-chain set of wallets Tethera has labelled automated. One slot per wallet
  holding the labelling timestamp. A `setter` (the labelling job's hot wallet) adds and removes in batches; the
  `owner` appoints the setter, can pause it, and hands over ownership in two steps. Anyone can read it.
- `src/CrowdingHook.sol`: a v4 hook for dynamic-fee pools. Per pool it remembers the last 32 flagged swaps
  (wallet, block, side), counts distinct flagged wallets per side inside a window of W blocks, and calls a herd
  when one side has at least H and the other side is at most 15% of that. Each swap gets its own LP fee through
  the dynamic-fee override: base for calm, base plus the LPs' share of the surcharge for flow joining the herd,
  base minus the rebate (floor 1 bp) for flow leaning against it. Tethera's share is 20% of the surcharge only,
  a constant capped at 20% in code, taken on the unspecified side after the swap and sent to `treasury`. The
  pool's creator (whoever initialised it) tunes base, surcharge, rebate, W, H and the opposite-side share inside
  the hook's bounds. `herdState` and `feeFor` let the API quote the fee before a trade. `CrowdFee` is emitted for
  every surcharged or rebated swap.

The wallet a swap is attributed to is `tx.origin`, because that is what the off-chain labelling keys on and the
`sender` a hook receives from the pool manager is the router.

## Build and test

```
curl -L https://foundry.paradigm.xyz | bash && foundryup
cd contracts
forge install uniswap/v4-core --no-git     # lib/ is not committed
forge build
forge test
```

`lib/` is fetched by `forge install` and ignored by git. The project compiles with solc 0.8.26, cancun, via-IR
and the same optimizer setting as v4 core (the pool manager does not compile under a smaller one).

## Deviations from the spec and what is deliberately not done

- Tethera's cut is taken through `afterSwapReturnDelta` on the unspecified side, not `beforeSwapReturnDelta`.
  The after-swap pattern is the one v4 core ships a reference test for, and it measures the cut on the realised
  amount. The LP fee override itself is per swap in `beforeSwap` as specified.
- The base fee is a hook parameter (default 30 bp) rather than read from the pool's stored dynamic fee.
- Per-swap state between `beforeSwap` and `afterSwap` is a storage slot, not transient storage. Cheap to change.
- The ring holds 32 distinct wallets per pool, so H is capped at 32; counting is a 32-slot read per swap.
  An incremental counter with lazy expiry is the v2 gas work.
- No Sybil mitigation (unflagged repeat senders at half weight) yet, as the spec says for v1.
- Not done: deployment scripts, a multisig owner, the testnet replay, and the API's use of `feeFor`.
