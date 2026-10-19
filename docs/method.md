# Method: how Tethera classifies trading on Robinhood Chain

This is the rule book. The code that implements it is in this repository (`scripts/build_features.py`, `config/labels.toml`, `sql/017_self_cancelling.sql`, `scripts/fetch_token_meta.py`, `scripts/launch_audit.py`). Every number Tethera publishes is produced by these rules and can be re-run by anyone with a node and the code. Changes to the rules are listed at the end with their dates.

## 1. What we read

Every Uniswap v2, v3 and v4 swap on Robinhood Chain (chain 4663), decoded within seconds of the block, with the transaction's sender. We keep the last seven days hot and the full history since mainnet in an archive. Nothing is sampled.

## 2. What we remove before counting anything

- **Self-cancelling trades.** A transaction that swaps both ways in one pool, with both token amounts netting to under 1% of the gross. Under Uniswap v4's flash accounting such a transaction settles nothing. These swaps stay in our tables with a flag and are excluded from every aggregate, label, herd count and published volume. We report their size separately.
- **Copycat tokens.** A symbol is cheap to copy. For the stock, stable and eth kinds, one token per symbol keeps the kind: the one in the most pools, earliest deployed on a tie. The rest are marked copies. A stablecoin must also sit in at least 20 pools. For stocks, Robinhood's own Stock Token registry is the authority: only its contracts count as official.
- **Volume bots.** Wallets with no net position (net flow under 10% of gross), at least 80% of their swaps in one pool, and a fixed daily budget (daily volume coefficient of variation under 0.35 once active three days or more), over 20 or more swaps. They are automated, but they are not a herd, so they are excluded from herd counts.

## 3. What makes a wallet "automated"

A wallet is judged on its last seven days of swaps, self-cancelling trades removed, and only if it has at least five of them. **One transaction is one trade**: an aggregator routes a person's single swap through several pools in one block, so timing is measured between transactions, never between the legs of one.

Seven signals:

| signal | fires when | needs |
|---|---|---|
| several pools in one block | the wallet traded in more than one pool in one block, in separate transactions, at least once | |
| sub-second gap | the shortest gap between two of its transactions is under 1.0 second | |
| metronomic | its gaps between transactions are regular: coefficient of variation under 0.5 | 10+ swaps |
| fast round trips | at least 3 buy-then-sell (or sell-then-buy) in the same pool within 30 seconds, in separate transactions | |
| computed sizes | at most 5% of its amounts are round numbers | 10+ swaps |
| reactive | at least half of its swaps land within two blocks after another wallet's swap in the same pool | 10+ swaps |
| persistent | 20+ swaps over a span of at least 30 minutes with no idle stretch longer than 10 minutes | |

The first two signals count as one, because several pools in one block implies a zero gap (81% overlap in calibration). A wallet is **automated** when three or more independent signals fire. Nothing about the wallet's name, registry entry or contract code is used to make the label.

## 4. What a herd is

A **busy minute** is one pool, one minute, with fifteen or more automated wallets (volume bots excluded) on one side. A **herd** is a busy minute where the other side has at most 15% of that number. Two-sided busy minutes are usually arbitrage keeping the pool in line with other markets; they are counted and reported, but they are not herds.

## 5. What checks the labels

- **The ERC-8004 agent registry is the test set, not the label.** Registered agents that trade are scored against the labels; they are never used to make them. Registered agents barely trade (57 of 8,792 at the time of writing), which is why a registry alone would miss almost every bot.
- **Calibration after every full run**: for each signal, how often it fires among judged wallets, how often it fires alone on wallets sitting exactly on the threshold, and the overlap between signals. The current report is published with the weekly letter.
- **Reproduction.** Pool ids, transaction ids and the window are given with every published number.

## 6. The launch audit

For a token over a window, in addition to the above: the share of wallets whose first swap in our data fell in this token's window; the share with exactly one swap; the largest batch of such wallets whose first swap landed in one ten-minute bucket; the ten busiest wallets' share of swaps; the share that only ever bought or only ever sold; and, from the chain itself, the share of wallets whose total transaction count fits inside the swaps we saw them make here plus two (a funding transfer and an approval), which we call **born for this**. Verdicts: organic, mixed, automated, fresh-wallet driven, printed. The rules for each are in the code and in the audit's own notes.

## 7. Known limits

- A wallet with fewer than five swaps in seven days is not judged. Launch bots that trade once are caught by the launch audit's fresh-wallet columns, not by the label.
- Most automated labels sit exactly on the three-signal threshold, so rule changes move the count. We publish the count before and after every change.
- A slow bot, one trade an hour, trips nothing.
- "First swap in our data" is measured from 10 October 2026 until the archive is indexed; the on-chain transaction count does not have that limit.
- The test set is small. We are building a larger labelled set of known bots and known human flows and will publish precision and recall when it exists.

## 8. Change log

- **14 Oct 2026.** First rules: seven signals, three to label, five swaps minimum, seven-day window.
- **17 Oct 2026.** Volume-bot filter added; volume bots excluded from herd counts.
- **18 Oct 2026.** Several-pools-in-one-block and sub-second-gap count as one signal (81% overlap). Self-cancelling trades flagged and excluded everywhere. One canonical token per symbol; stables need 20 pools; Robinhood's registry decides which stock tokens are official. The ten-minute job judges active wallets on the same seven-day window as the weekly run (it had been judging on six hours).
- **19 Oct 2026.** One transaction is one trade: timing and round-trip signals no longer fire on the legs of a routed swap. Measured effect on the live week: automated wallets 11,830 → 6,613 of 247,391 judged; share of all swaps 43% → 35%; one-sided herd minutes about 1,540 a day before and after (herds are made by the large bots, which kept their label); busy minutes 6,800 → 5,960 a day; the hook simulation's surcharge unchanged at $803k on the week.
