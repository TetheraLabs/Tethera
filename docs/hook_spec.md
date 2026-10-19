# Crowding-aware v4 hook — specification (draft, 18 Oct 2026)

Status: draft for approval before any Solidity. Numbers from `reports/hook_fee_sim.md` (live week 11–18 Oct 2026, stable-quoted pools) and the netting simulator.

## 1. What it does

A Uniswap v4 hook that sets the swap fee per swap from the crowd in the pool right now:

- **Calm:** the pool's base fee.
- **Herd side** (flow joining a forming herd): base + surcharge.
- **Leaning side** (flow against a forming herd): base − rebate.

The pool keeps the dynamic-fee flag (`0x800000`, already set on 230,000 pools on this chain, 17% of stable-quoted volume), so a dynamic fee is normal here; the hook only changes what drives it.

## 2. What the week of data says

Live week to 18 Oct 2026, $2.99B of stable-quoted volume after dropping copycat "stablecoins", copycat-quoted pools and static zero-fee pools (97% of whose volume was same-transaction round trips that settle nothing under v4 flash accounting). Labels: the 7-day labelling on the rule of 19 October (one transaction is one trade); the rule change removed 5,200 small false positives and left the herd numbers within 2% of the day before. 93% of automated labels sit exactly at the three-signal threshold, so these numbers are sensitive to the label rule; the calibration report tracks that.

| | flat 30 bp | adjusted (+20 / −10 bp) | adjusted, judged one minute late |
|---|---|---|---|
| Fee revenue | $9.06M | $9.85M (+8.8%) | $9.32M (+3.0%) |
| Surcharge collected | – | $803k | $324k |
| Rebates paid | – | $10.4k | $55.8k |
| Herd-side volume | – | $402M (13.3% of all) | $162M |

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
- Tethera's slice: **20% of the surcharge only, 0% of the base fee** (decided 18 Oct 2026; 25% was proposed, 20% chosen for adoption), taken through `beforeSwapReturnDelta`. The share is a constant in the hook with a hard cap of 20% in code, so it can only go down without a redeploy. LPs keep the whole base fee and four fifths of the surcharge; Tethera earns only when the hook does something. On the week's 7-day-label numbers, chain-wide: $161k to Tethera, $642k extra to LPs, $9k rebated to leaning flow; at launch scale (one or two pools) a few dollars a week.
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

## 7. Decisions and open points

- **Decided 18 Oct 2026:** the automated-wallet set is public on-chain (`AutomatedSet`), readable by any hook or contract; Tethera's share is 20% of the surcharge only.

- **Week-2 outcome study, corrected 19 Oct 2026 (reports/outcome_study.md).** Over 1 Jul to 17 Aug, a one-sided crowd of 15+ against solo trades of the same kind: on ETH/USDG hub pools no cost (entry 5 bp better, +2 bp at 1 h, −7 bp at 24 h); on stock-token pools no entry cost and positive follow-through (+155 bp at 1 h, +1,047 bp at 24 h, 725 crowd cells); on memecoin pools a large cost: the crowd paid a median 261 bp more at entry and was a median 12% down an hour later, 8% down a day later, winning 44% of the time (26,685 crowd cells). So the hook's surcharge case is memecoin and launch pools, which is where Pons and Hookr create them; on the hub it is a protection story only.
- H and W want a block-level replay rather than minute aggregates; the testnet replay gives that.
- Whether pool creators (the launchpad) will accept a hook they do not control: the params are theirs, the set is ours; the set is public so they can audit who is in it.
