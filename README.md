# Tethera

A verifiable systematic research desk for tokenised equities on Robinhood Chain.

The desk searches for trading strategies automatically, scores them under a locked rule on years chosen before the search, validates them on years the search never saw, and keeps an append-only, hash-chained record of every trial, every improvement and every read of sealed data. The strategies are private. The protocol, the price construction, the record and the dictionary of what the search can reach are public, so the record can be checked rather than believed.

- **Method**: `docs/method.md`. The year rule, the scorer, validation, the ledger, the research director, and the chain data layer under it all.
- **Parameter dictionary**: `docs/parameters.md`. Every parameter the search can change, grouped by the concept it comes from, with sources.
- **API**: `docs/api.md`. The desk record and board, keys and access by burning the token, and the wallet and pool routes of the data layer.
- **Prices**: `src/tethera/chain_bars.py` turns on-chain swaps into one-minute bars. Anyone with the swap tables rebuilds the same bars.
- **Verification**: `scripts/verify_record.py` recomputes the record's hash chain from a served feed.

Nothing has survived validation as of 24 October 2026. The record shows that too.

## Layout

| Path | What it is |
|---|---|
| `src/tethera/` | API, keys and access, the chain-bar builder, the instrument table, swap decoding and RPC client, wallet profiles |
| `scripts/` | The data layer's jobs: followers, backfill, pool and token metadata, wallet features and labels, health check, key administration, the record verifier |
| `sql/` | Every migration, in order |
| `config/` | Chain constants and the label rules |
| `docs/` | Method, API, parameter dictionary |
| `site/` | The public page |
| `tests/` | Smoke and invariant tests against a running stack |

The search engines, the strategies, the ledger file, the research director and the operations folder are not in this repository.

## Running the data layer

Copy `.env.example` to `.env` and fill in the database URL and RPC endpoints. The followers write every swap with its sender into `swaps`; the nightly archive job moves swaps older than fourteen days to `swaps_archive`; the labelling job computes wallet features and automated labels every ten minutes; the bar builder advances `desk_bars_1m` by watermark every five minutes. Each job is a plain script under `scripts/` or a module under `src/tethera/`, run by the host's service manager from a private operations folder.

`curl localhost:18790/v1/health` reports the data's freshness. `.venv/bin/python -m pytest -q tests` runs the tests against the live stack.

## Licence

See `LICENSE`.
