# Crowding-aware v4 hook — specification (draft, 18 Oct 2026)

Status: draft for approval before any Solidity. Numbers from `reports/hook_fee_sim.md` (live week 11–18 Oct 2026, stable-quoted pools) and the netting simulator.

## 1. What it does

A Uniswap v4 hook that sets the swap fee per swap from the crowd in the pool right now:

- **Calm:** the pool's base fee.
- **Herd side** (flow joining a forming herd): base + surcharge.
- **Leaning side** (flow against a forming herd): base − rebate.

The pool keeps the dynamic-fee flag (`0x800000`, already set on 230,000 pools on this chain, 17% of stable-quoted volume), so a dynamic fee is normal here; the hook only changes what drives it.

## 2. What the week of data says

Live week to 18 Oct 2026, $2.99B of stable-quoted volume after dropping copycat "stablecoins", copycat-quoted pools and static zero-fee pools (97% of whose volume was same-transaction round trips that settle nothing under v4 flash accounting). Labels: the first consistent 7-day labelling of the week (18 Oct 18:26; earlier versions of this table sat on six-hour judgements that missed the large arbitrage bots and showed 1.3% herd volume). 93% of automated labels sit exactly at the three-signal threshold, so these numbers are sensitive to the label rule; the calibration report tracks that.

| | flat 30 bp | adjusted (+20 / −10 bp) | adjusted, judged one minute late |
|---|---|---|---|
| Fee revenue | $8.95M | $9.74M (+8.9%) | $9.22M (+3.0%) |
| Surcharge collected | – | $803k | $317k |
| Rebates paid | – | $9.3k | $48.5k |
| Herd-side volume | – | $401M (13.5% of all) | $159M |

- Herd minutes are 13.5% of volume on the 7-day labels (8,718 herd minutes in the week), so the adjusted schedule lifts LP revenue by about 9%. The adverse-selection side is smaller than the fee side: the price moves the herd's way by a median +5 bp over the next five minutes, and the move × herd volume ($359k) is 30% of the flat fees earned on that volume ($1.20M). The honest pitch is still the herd's own cost and the LP's protection in the worst minutes, with a revenue uplift that is now material rather than a rounding error. At a flat fee a herd minute is close to worthless for LPs; the surcharge turns it profitable.
- For the herd the pitch is the netting simulator's number: in herd minutes the AMM price was ~30 bp worse than a Dutch fill. Joining a herd already costs 30 bp; the surcharge makes that explicit and the rebate pays whoever absorbs it.
- **Judging one minute late loses 60% of the surcharge** ($803k → $317k). Whatever counts the herd must see it within seconds, not minutes.

Sensitivity is linear: every 10 bp of surcharge is +4.5% revenue; every 5 bp of rebate costs 0.05%.

## 3. Decision: how the hook knows who is in the herd

Tested and rejected: **counting distinct senders in-hook.** One-sided crowds of 15+ wallets are overwhelmingly people (9,682 such minutes in the week against 359 automated herds); the best threshold reaches 13% precision. A plain count would tax retail rushes and miss the agents.

Chosen: **an on-chain set of automated wallets, maintained by Tethera, counted in-hook.**

- `AutomatedSet` contract: `mapping(address => uint64 labelledAt)`; Tethera's hot wallet adds and removes wallets from the ten-minute labelling run (deltas are a few hundred a day; one SSTORE each, cheap on this chain). Public and readable by anyone: it is the attestation layer the plan already wants, and the hook is its first consumer.
- The hook keeps, per pool, a ring buffer of the last W blocks (W = 600, about 60 s at 10 blocks/s) of (block, sender, side) for senders in the set, and counts distinct flagged senders per side. Herd when one side ≥ H flagged wallets and the other side ≤ 15% of it. H calibrated to the off-chain definition (15 wallets a minute); start at H = 10 over 60 s and tune on testnet replay.
- Zero lag: the count is as of the current block. Cost: two or three storage writes per flagged swap, none for unflagged senders beyond one SLOAD.
- Sybil: splitting a herd across fresh wallets evades the set until they are labelled (five swaps, next ten-minute run). Mitigation for v2: count unflagged senders that have already swapped in this pool within the window at half weight. Not in v1.

The hot wallet also posts nothing per-pool per-minute; the earlier "posted score" design is dropped because of the lag result.

## 4. Fee mechanics

- `beforeSwap` returns the LP fee override for this swap (`OVERRIDE_FEE_FLAG | fee`), so the surcharge and the rebate are per swap, not per pool state. No `updateDynamicLPFee` churn.
- Base = the pool's configured dynamic base (set at creation, e.g. 25 or 30 bp). Surcharge 20 bp, rebate 10 bp, floor at 1 bp. All three are pool parameters set by the pool creator, bounded by the hook (surcharge ≤ 100 bp, rebate ≤ base).
- Tethera's slice: **a share of the surcharge only** (proposal: 25% of surcharge, 0% of base), taken through `beforeSwapReturnDelta`. LPs keep the whole base fee and most of the surcharge; Tethera earns only when the hook does something. On the week: $11.6k to Tethera, $34.7k extra to LPs, $3.8k rebated to leaning flow. *Needs the founder's approval.*
- Rebates are funded from the base fee of the same swap (the leaning swap simply pays less); no treasury, no claims.

## 5. Contracts

1. `AutomatedSet` — owner (Tethera multisig / Ledger), `setter` (hot wallet), `add(address[])`, `remove(address[])`, `labelledAt(address)`, events. Pausable setter.
2. `CrowdingHook` — v4 `BaseHook`; permissions: `beforeSwap`, `beforeSwapReturnDelta`, `afterInitialize` (reads and validates pool params). Per-pool params struct. Ring buffer per pool. View functions `herdState(poolId)` and `feeFor(poolId, sender, zeroForOne)` so the API and the fill receipts can quote the fee before trading.
3. Fill receipts (plan item): `afterSwap` emits `CrowdFee(poolId, sender, side, fee, herdCount)`; the API turns the event into "paid N bp less than the crowd".

## 6. Testnet plan (Robinhood Chain testnet 46630)

1. Deploy `AutomatedSet`, seed it with the live automated set (9.3k addresses, batched).
2. Deploy `CrowdingHook` at a mined address with the right permission bits; create one ETH/USDG demo pool with the dynamic-fee flag and small liquidity.
3. Replay: a script drives 20 flagged wallets through the pool in one direction over 30 s, then 5 unflagged; assert the fee path (base → base+20 on the herd side, base−10 on the other side, back to base after W blocks).
4. Gas report per swap path; target under 60k gas extra on the flagged path.
5. Review, then one seeded mainnet demo pool; pitch the launchpad and one stock-token issuer with the week-2 number.

## 7. Open points

- The week-2 outcome study (24 h horizon) decides the headline: if crowding costs agents measurably over a day, the hook ships with the surcharge; if not, it ships on the five-minute adverse-selection number alone, which is weaker.
- H and W want a block-level replay rather than minute aggregates; the testnet replay gives that.
- Whether pool creators (the launchpad) will accept a hook they do not control: the params are theirs, the set is ours; the set is public so they can audit who is in it.
