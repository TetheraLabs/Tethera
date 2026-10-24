# Method

Tethera is a systematic research desk for tokenised equities on Robinhood Chain. It searches for trading strategies automatically, scores them under a locked rule on years chosen before the search, validates them on years the search never saw, and keeps an append-only, hash-chained record of every trial, every improvement and every read of sealed data. The strategies are private; the protocol and the record are public. This page is the protocol. The chain data layer that produces the prices is in part B.

## Part A. The desk

### 1. Prices

Every instrument is priced from the chain's own fills: every swap in every pool that pairs the official Robinhood stock token with USDG, aggregated into one-minute bars (`src/tethera/chain_bars.py`). Self-cancelling swaps are dropped; within a minute, prints more than 15% from that minute's median are dropped as bad fills. Anyone with the swap tables rebuilds the same bars; the rule is the SQL in that file and nothing else. Before the chain feed begins, the same instruments are carried by their own history, scaled forward only at the join so no past bar is ever rewritten by a level known later.

### 2. The year rule

Training years are a recency-weighted random draw of seven years from 2012 to 2025, with 2024 and 2025 always included; the draw's seed is written next to the run. The years the draw left out are validation years. 2026 is sealed: a registered candidate may read it once, on chain prices, and the ledger refuses a second read. Data that exists only on the chain (automated share, self-cancelling share, fleet presence) runs on a declared split of its own, three months of training and October 2026 for validation, and is labelled as such wherever it is shown.

### 3. Scoring

A candidate's score is its mean yearly net return at 15 basis points one-way, multiplied by the share of training years that were positive. Every candidate is also run at 5 and 40 basis points. A floor on entries per year is a run setting, recorded with the run; in the baseline phase it is zero, so the search decides how often to trade. Fills are at the next bar's open. Nothing is ever selected on a raw Sharpe ratio.

### 4. Validation

When a search ends, its finalists, the best candidate of each distinct structure, are rescored once on the validation years. Each finalist's monthly returns give a deflated Sharpe ratio (Bailey and López de Prado) discounted by the number of candidates the run scored; the finalists together give a probability of backtest overfitting (Bailey, Borwein, López de Prado and Zhu) over yearly blocks. A survivor needs a positive training score, a positive validation mean year, at least half the validation years positive and a deflated Sharpe probability of 0.95. Survivors then face a stress battery, a parameter neighbourhood test, multiple seeds and the other instruments.

### 5. The ledger

Every candidate any engine scores is one line in an append-only ledger. Every time a search's best moves, the step is logged with the parent, the mutation, the parameter diff and the score change. A candidate is registered by the hash of its definition before any sealed data is touched; the sealed read, the admission and any withdrawal are further lines. The desk record served by the API (`GET /v1/desk/record`) is hash-chained row to row; `scripts/verify_record.py` recomputes the chain from a served feed.

### 6. The search engines

Four engines score under the same rule: a structural search over a library of signal families drawn from the parameter dictionary (`docs/parameters.md`), a code-writing agent, a formula miner with a false-discovery gate, and a parameter search over an older strategy lineage. A research director reads the ledger and the trails once a day, reads the literature, proposes new families as code (each must pass a causality probe by truncation before it enters the library) and sets the next runs. It has no write access to the scorer, the year rule, the admission gate or the ledger, and their hashes are checked before and after each of its sessions.

### 7. Going live

No strategy appears on the paper board, the API or anywhere public until it is a survivor that has also passed the full battery. Paper agents are priced from the chain bars with a fixed slippage until a measured number replaces it; their fills enter the record. Real capital comes only after a paper record, and each fill then carries a transaction hash.

### 8. Known limits

- Two instruments have history before 2026; the other eight are reachable only through the chain-native split, which is three months of training.
- 15 basis points one-way is an assumption until slippage is measured on the chain's own fills.
- Fills are at the next bar's open, so close-to-open (overnight) rules cannot yet be expressed.
- Nothing has survived validation as of 24 October 2026. The record shows that too.

## Part B. The chain data layer

### 1. What we read

Every Uniswap v2, v3 and v4 swap on Robinhood Chain (chain 4663), decoded within seconds of the block, with the transaction's sender. The last fourteen days are kept hot and the full history since mainnet in an archive.

### 2. What we remove before counting anything

- **Self-cancelling trades.** A transaction that swaps both ways in one pool, with both token amounts netting to under 1% of the gross. Under Uniswap v4's flash accounting such a transaction settles nothing. These swaps are flagged and excluded from bars, volumes and labels.
- **Copycat tokens.** For the stock, stable and eth kinds, one token per symbol keeps the kind: the one in the most pools, earliest deployed on a tie. The rest are marked copies.
- **Volume bots.** Wallets with no net position, almost all their swaps in one pool, and a fixed daily budget.

### 3. What makes a wallet "automated"

A wallet is judged on its last seven days of swaps, self-cancelling trades removed, only if it has at least five of them. One transaction is one trade. Seven signals: several pools in one block, sub-second gap (these two count as one), metronomic gaps, fast round trips, computed sizes, reactive timing, persistence. Three or more independent signals make the label. The rules and thresholds are in `config/labels.toml` and `scripts/build_features.py`; the ERC-8004 agent registry is a test set for the labels, never an input to them. The automated share of a minute's swaps is one of the chain-native columns the desk searches.

### 4. Wallet profiles

`GET /v1/wallet/{address}/profile` gives any wallet's age, activity, labels, family and early entries into new pools, from our own tables. `GET /v1/wallet/{address}` gives the fingerprint behind an automated label.

### 5. Change log

- **14 to 19 Oct 2026.** Label rules, volume-bot filter, self-cancelling flag, one canonical token per symbol, one transaction is one trade.
- **21 Oct 2026.** The desk: year rule, locked scorer, ledger, pre-registration, sealed 2026, access by burning the token.
- **23 Oct 2026.** Structural search over the family library; baseline phase without a trade floor; improvement trail; paper-agent machinery (dormant until a survivor exists).
- **24 Oct 2026.** Research director; chain-bar builder and record verifier published.
