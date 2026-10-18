# Tethera

A pre-trade crowding check for AI trading agents: before executing, an agent asks privately how many other agents are making the same trade right now, and gets an answer in milliseconds.

Thousands of agents run on the same few foundation models and reach the same conclusions at the same moment. Tethera measures that herding on-chain, starting with Robinhood Chain.

## Operations

Everything live runs as a launchd user agent on the machine that hosts the database. The plists in `launchd/` carry a `__REPO__` placeholder; `scripts/install_launchd.sh` fills in the checkout path, copies them to `~/Library/LaunchAgents/` and loads them. Logs are in `data/`.

| Job | What it does | Log |
|---|---|---|
| `com.tethera.follow-live` | Polls the chain head and writes every Uniswap swap, with its sender, into `swaps` | `follow_live.log` |
| `com.tethera.follow-uniswapx` | Records `Fill` events from the UniswapX reactor into `uniswapx_fills` | `follow_uniswapx.log` |
| `com.tethera.follow-uniswapx-orders` | Polls the UniswapX order feed into `uniswapx_orders` | `follow_uniswapx_orders.log` |
| `com.tethera.labels` | Every 10 min: wallet features, automated labels, per-minute rollup | `labels.log` |
| `com.tethera.api` | HTTP API on 127.0.0.1:18790 (`/v1/health`, `/v1/crowding`, `/v1/herds`, `/v1/pool/{pool}`, `/v1/wallet/{address}`; rate-limited, keys in `api_keys`; see `docs/api.md`) | `api.log` |
| `com.tethera.healthcheck` | Every 5 min: lag, jobs, gaps, disk; alerts to `alerts.log` and a desktop notification | `healthcheck.log`, `health.jsonl` |
| `com.tethera.archive` | Nightly 03:00: moves swaps older than 14 days to `swaps_archive` on the external drive | `archive.log` |
| `com.tethera.backup` | Nightly: Postgres dump to the external drive, 14-day retention | `backup.log` |

Restart one: `launchctl kickstart -k gui/$(id -u)/com.tethera.follow-live`. Install or reinstall one: `scripts/install_launchd.sh com.tethera.follow-live` (no argument installs all of them). `launchctl list | grep tethera` shows pids.

**RPC failover.** The followers use `tethera.rpc.live_rpc()`: the paid endpoint (`PAID_RPC_URL`) first, the public endpoint (`RPC_URL`) when it answers 401/403/429/5xx or stops responding. A failed endpoint is retried after 10 minutes. The switch is logged as `[rpc] switched to <host>`.

**Gaps.** The live follower only reads the most recent blocks; if it falls more than 60 blocks behind it skips ahead and logs `GAP <lo> <hi>`. Fill the gap from HyperSync with `python scripts/gapfill_hypersync.py --from <lo> --to <hi>`, or `--auto` to find and fill every gap over 100 blocks in the last two days. The health check reports gaps in the last six hours.

**Health.** `curl localhost:18790/health` returns the lag in seconds and blocks, the endpoint in use and the last health-check result. `data/alerts.log` holds every problem the check has seen; a desktop notification is shown at most once per problem per hour.

**MCP.** The crowding signal is also an MCP server over stdio for trading agents: `PYTHONPATH=__REPO__/src __REPO__/.venv/bin/python -m tethera.mcp_server`, with `__REPO__` the checkout path. Tools: `crowding`, `herds`, `wallet`, `health`. The client configuration snippet is in `docs/api.md`.
