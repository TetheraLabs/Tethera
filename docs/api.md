# Tethera API

A pre-trade crowding check for automated traders on Robinhood Chain. Read-only endpoints, JSON, no authentication needed; keys raise the rate limit. The same signal is available as an MCP server for agents.

Base path: `/v1/`. The interactive schema is at `/docs`.

## GET /v1/crowding

How crowded one side of one pool is right now, over the last 60 seconds.

| parameter | required | meaning |
|---|---|---|
| `pool` | yes | Pool address (Uniswap v2/v3) or 32-byte pool id (v4) |
| `direction` | yes | `sell0` = selling token0 into the pool, `buy0` = buying token0 |
| `size` | no | Intended size in token0 units. Accepted and echoed back; a later version will use it |

```json
{
  "pool": "0x52e65B17fB6E5BA00Ed806f37Afcd2DaA50271Ca",
  "direction": "buy0",
  "as_of": "2026-10-17T20:51:03+00:00",
  "window_seconds": 60,
  "auto_wallets_same": 23,
  "auto_wallets_opposite": 2,
  "all_wallets_same": 31,
  "auto_swaps_same": 41,
  "one_sidedness": 0.92,
  "pools_moving_same_way": 3,
  "percentile": 0.987,
  "verdict": "herd",
  "explanation": "23 automated wallets this way vs 2 the other way; one-sided",
  "size": null
}
```

- `auto_wallets_same`: automated wallets that traded this side of this pool in the last 60 s. `auto_wallets_opposite`: the other side. Wallets are labelled automated by behaviour (sub-second gaps, same-block multi-pool trades, metronomic timing, fast round trips, computed sizes, reacting within two blocks, persistence); wallets that run paid volume services are automated but never count here.
- `one_sidedness`: same ÷ (same + opposite). 0.5 is balanced, 1.0 is everyone the same way.
- `pools_moving_same_way`: pools chain-wide with at least 10 automated wallets on this side in the window. Several at once suggests a shared signal rather than a local event.
- `percentile`: where `auto_wallets_same` sits in this pool's own per-minute history for this direction. 0.95 means busier than 95% of past minutes. Null when the pool has no history yet.
- Swaps that cancel themselves inside one transaction (both ways in one pool, nothing settled) are flagged at ingest and never counted, here or anywhere else. In the week to 18 Oct 2026 they were a quarter of the chain's apparent stable-quoted volume.
- `verdict`: `herd` (15+ automated wallets one way, the other side at most 15% of that), `busy` (15+ but two-sided, or above the 95th percentile), `clean`.

## GET /v1/herds

Every pool with a one-sided automated crowd in the last minute, biggest first.

| parameter | required | meaning |
|---|---|---|
| `min_wallets` | no | Crowd size threshold, 5 to 500. Default 15 |

```json
{"herds": [{"pool": "0x52e65B17…", "direction": "buy0", "auto_wallets_same": 23, "auto_wallets_opposite": 2, "as_of": "2026-10-17T20:51:03+00:00"}]}
```

## GET /v1/wallet/{address}

The behavioural fingerprint of one wallet: what it behaves like, not what it is. Covers wallets labelled automated or registered as ERC-8004 agents with at least 10 swaps in the last week, recomputed nightly. Address case does not matter. `404` when the wallet has no fingerprint.

```json
{
  "wallet": "0x50773316F6e8495dcC9F89dc679619b75a6B58EA",
  "computed_at": "2026-10-18T11:19:10+00:00",
  "window_start": "2026-10-11T11:09:39+00:00",
  "window_end": "2026-10-18T11:09:39+00:00",
  "is_automated": true,
  "is_registered_agent": false,
  "signals": [
    "sub_second_gap",
    "fast_round_trips",
    "computed_sizes"
  ],
  "archetype": {
    "id": 1,
    "label": "seconds-apart cadence, one pool per block, one dominant pool, few tokens",
    "description": "seconds-apart cadence (median gap 8.5 s, gap CV 2.25, 30% of swaps within 2 s of the last); few pools (4 pools, 2 tokens, top pool 50% of swaps, stock 0%, stable pairs 0%); computed sizes (0% round, 0% repeated); trades around the US open, weekdays only (hour entropy 0.92 of 3.18, busiest block 15-19 UTC holds 93%, weekend 0%); reactive 13%, 26.3 round trips and 0.0 multi-pool blocks per 100 swaps, net flow 0.10; about 23 swaps a week."
  },
  "family": {
    "id": 30,
    "size": 6
  },
  "nearest_agents": [
    {
      "name": null,
      "wallet": "0xa26CC5fD86DeF0050de3EF43dd4796DAe89Ca25C",
      "agent_id": 6524,
      "distance": 0.5591
    },
    {
      "name": null,
      "wallet": "0xf5B4D097042cF2AE5bDB8EEB9E3a836875421854",
      "agent_id": 7598,
      "distance": 0.8284
    },
    {
      "name": null,
      "wallet": "0x0950EC35e3358372CCDD5CCA243D5bf7230AB8Cb",
      "agent_id": 8644,
      "distance": 0.857
    }
  ],
  "hour_hist": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 35, 0],
  "weekday_hist": [0, 0, 0, 0, 35, 0, 0],
  "n_swaps": 35,
  "median_gap": 3.5,
  "gap_cv": 2.2173,
  "min_gap": 0.0,
  "period_s": null,
  "fast_share": 0.3235,
  "hour_entropy": -0.0,
  "busiest_block_start": 19,
  "busiest_block_share": 1.0,
  "weekend_share": 0.0,
  "us_hours_share": 0.0,
  "n_pools": 1,
  "top_pool_share": 1.0,
  "stock_share": null,
  "stable_share": null,
  "other_share": null,
  "known_pool_share": 0.0,
  "n_tokens": 0,
  "round_amount_frac": 0.0,
  "repeat_amount_frac": 0.0,
  "log_size_median": null,
  "log_size_iqr": null,
  "reactive_share": 0.4571,
  "round_trips_per_100": 17.1429,
  "net_flow_frac": 0.4141,
  "multi_pool_per_100": 0.0
}
```

- `archetype`: the cluster of behaviour the wallet falls into, with a label and description generated from the cluster's median cadence, venues, sizing and hours. An archetype is not a model or a framework; nothing on the registry names one.
- `family`: the correlated family from the nightly pairs job (wallets that keep trading the same pool, minute and side together) and its size. Null when the wallet is not in one.
- `nearest_agents`: the three registered agents closest in the standardised feature space, by cosine distance, with the registry name when the profile has one. Few registered agents trade enough to be in the set, so read this as "closest of a small set".
- Cadence: `median_gap`, `min_gap` and `period_s` in seconds (`period_s` is set only when the gaps are metronomic, gap CV below 0.5); `fast_share` is the share of swaps within 2 s of the previous one.
- Activity: `hour_hist` (24 bins, UTC) and `weekday_hist` (7 bins, Monday first); `hour_entropy` in nats (3.18 is uniform over the day); `busiest_block_start` is the start hour of the busiest 4-hour block and `busiest_block_share` what it holds; `us_hours_share` is 13:30 to 20:00 UTC.
- Venue: `stock_share`, `stable_share`, `other_share` by the pool's tokens, over all swaps; `known_pool_share` says how many swaps were in pools whose tokens are known, so the three shares under-count when it is low.
- Sizing: `round_amount_frac`, `repeat_amount_frac`; `log_size_median` and `log_size_iqr` are log10 of the trade size in token1 units and are only measured where token1 is a stable coin, else null.
- Behaviour: `reactive_share` (swaps that follow another wallet's swap in the same pool within two blocks), `round_trips_per_100`, `multi_pool_per_100` (blocks with swaps in more than one pool), `net_flow_frac` (|net| ÷ gross token0, 0 is pure churn).

## GET /v1/wallet/{address}/profile

A reputation profile of any address that has swapped on the chain's Uniswap pools, not only labelled ones. It says what the address has done here, never who holds it. `404` when we hold no swaps from it.

```json
{
  "wallet": "0x…",
  "first_seen": "2026-07-03T09:12:41+00:00",
  "first_seen_is_exact": true,
  "age_days": 109.5,
  "transactions_on_chain": 33490,
  "swaps_all_time_since_july": 18211,
  "last_10_days": {"swaps": 507, "pools": 65, "automated_swaps": 507, "self_cancelling_swaps": 0,
                   "last_swap": "2026-10-19T21:06:54+00:00",
                   "pools_entered_within_an_hour_of_creation": 18, "pools_with_known_creation": 52},
  "labels": {"is_automated": true, "is_volume_bot": false, "is_registered_agent": false, "is_contract": false,
             "signals": ["sub_second_gap", "fast_round_trips", "computed_sizes"], "labelled_at": "…"},
  "behaviour": {"archetype": "seconds-apart cadence, some stock pools, uniform ticket sizes, large tickets",
                "family_size": 3, "cluster_size": 3, "closest_partners": [{"wallet": "0x…", "minutes_together": 41, "jaccard": 0.62}]},
  "flags": ["automated", "early_in_launches", "in_a_family"],
  "reading": "Labelled automated by its swap pattern over the last week; entered five or more pools within an hour of their creation in the last ten days; keeps trading the same pool, minute and side as other wallets."
}
```

- `first_seen` is exact from 1 July 2026 once the wallet-age table is built from the archive (`first_seen_is_exact` true); until then it is the earliest swap in the live table, about ten days.
- `transactions_on_chain` is the address's transaction count from the chain, cached for seven days.
- `pools_entered_within_an_hour_of_creation` counts pools where the address's first swap came within 36,000 blocks (about an hour) of the pool's creation, over the live window.
- Flags, each with a fixed meaning: `contract` (the address has code), `registered_agent` (ERC-8004), `automated` and `volume_bot` (the nightly labels), `fresh` (first seen within 7 days and at most 3 transactions), `early_in_launches` (five or more early pool entries), `in_a_family` (a family or cluster of three or more), `self_cancelling` (30% or more of its swaps). `reading` is the flags in a sentence.

## GET /v1/pool/{pool}

Trust profile of one pool over the last 7 days: what traded, how much of it was real, and who was there. `pool` is the v2/v3 address or the 32-byte v4 pool id.

```json
{
  "pool": "0x52e6…", "protocol": "v3", "fee_tier": 0.01, "hooks": null,
  "token0": {"symbol": "WETH", "kind": "eth", "official_stock_token": false},
  "token1": {"symbol": "USDG", "kind": "stable", "official_stock_token": false},
  "priced_in": "USD",
  "last_7_days": {"swaps": 1509908, "peak_wallets_in_a_minute": 212, "automated_swap_share": 0.7371,
                  "organic_volume_usd": 1457381620.1, "self_cancelling_volume_usd": 545203.15,
                  "self_cancelling_swaps": 5814, "self_cancelling_senders": 414, "self_cancelling_share": 0.0004,
                  "herd_minutes": 38, "peak_automated_wallets_one_way": 450, "last_swap": "2026-10-18T18:33:30+00:00"},
  "flags": []
}
```

- `kind`: `stable`, `eth`, `stock`, `other`, or `copycat` (a token that shares its symbol with a busier one and is not it). `official_stock_token` is true only for contracts in Robinhood's own Stock Token registry.
- `priced_in` is `USD` when one side is a stablecoin; otherwise the volume fields are null and only counts are returned.
- `organic_volume_usd` excludes self-cancelling swaps: one transaction swapping both ways in one pool with both amounts netting to under 1% of gross, which settles nothing under v4 flash accounting. `self_cancelling_volume_usd` is that excluded volume, `self_cancelling_share` its share of the gross.
- `swaps` and `automated_swap_share` count organic swaps only. `peak_wallets_in_a_minute`: most distinct wallets seen in any one minute (a 7-day distinct count would need a full scan of the pool's swaps).
- `herd_minutes`: minutes with 15+ automated wallets one way and at most 15% of that the other way. `peak_automated_wallets_one_way`: the busiest such minute.
- `flags`: `zero_fee`, `dynamic_fee`, `quotes_copycat_token`, `mostly_self_cancelling` (over half the swaps), `single_sender_printing` (one or two senders account for over half the volume through self-cancelling trips).
- 404 for a pool we have never seen.

## GET /v1/health

`lag_seconds` and `lag_blocks` since the last swap the service has seen. Above about 60 seconds, treat the other two endpoints as stale. Not rate-limited.

## Rate limits

| caller | limit | burst |
|---|---|---|
| anonymous (per client address) | 30 requests a minute | 10 |
| with a key | set per key, default 300 a minute | limit ÷ 6, at least 10 |

Over the limit the response is `429` with `Retry-After` (seconds) and a JSON body `{"error": "rate limited", "retry_after_seconds": n, "limit_per_minute": n}`. Every response carries `X-RateLimit-Limit`.

Keys go in the `X-Tethera-Key` header. Only a SHA-256 hash of a key is stored server-side; a key that is unknown or disabled is treated as anonymous. Keys are issued by hand for now: write to TetheraLabs@proton.me.

The bare paths `/crowding`, `/herds` and `/health` still work as aliases of the `/v1/` ones.

## MCP server

The same calls (`crowding`, `herds`, `pool`, `wallet`, `wallet_profile`, `token`, `wait_for_calm`, `real_list`, `health`) as MCP tools over stdio, with descriptions written for an agent deciding whether to trade. Client configuration, with `__REPO__` replaced by the checkout path:

```json
{
  "mcpServers": {
    "tethera": {
      "command": "__REPO__/.venv/bin/python",
      "args": ["-m", "tethera.mcp_server"],
      "env": {"PYTHONPATH": "__REPO__/src", "DATABASE_URL": "postgresql://localhost/tethera"}
    }
  }
}
```

The MCP server reads the database directly, so it runs on, or with network access to, the machine that hosts it.

## GET /v1/token/{address}/badge.svg

An SVG badge with the token's seven-day verdict and automated share, cached 30 minutes. For a project page or a listing:

```html
<a href="https://tetheralabs.com/check.html#0x..."><img src="https://api.tetheralabs.com/v1/token/0x.../badge.svg" alt="Tethera real-activity check"></a>
```

## GET /v1/alerts and GET /v1/alerts/stream

One-sided herd minutes as they are detected: 15+ automated wallets one way in one pool in one minute, the other side at most 15% of that, volume bots and self-cancelling swaps excluded. Each alert has an increasing `id`, the `minute`, `pool`, `pair`, `direction`, `auto_wallets_same`, `auto_wallets_opposite`, `swaps` and `wallets`. The last thousand are kept.

- `/v1/alerts?after_id=N` returns alerts after id N (`0` for the most recent) and `last_id` to poll with.
- `/v1/alerts/stream` is a server-sent event stream (`event: herd`, `id:` per event, a keepalive comment every 15 seconds). Send `Last-Event-ID` to resume without a gap.

```
curl -N https://api.tetheralabs.com/v1/alerts/stream
```

## Keys and limits

| caller | limit |
|---|---|
| anonymous, by address | 30 requests a minute, burst 10 |
| free key | 120 a minute; `POST /v1/keys` with `{"label": "..."}`, three keys a day per address, no account |
| paid key | the tier's limit for 30 days; `GET /v1/keys/tiers` lists tiers and the treasury, `POST /v1/keys/paid` with `{"tx_hash": "..."}` after sending USDG on Robinhood Chain |

Send the key as `X-Tethera-Key`. Only a hash of it is stored. Paid tiers open when the treasury address is published.

## MCP

The same tools (`crowding`, `herds`, `pool`, `wallet`, `wallet_profile`, `token`, `wait_for_calm`, `real_list`, `health`) are served over the Model Context Protocol at `https://api.tetheralabs.com/mcp` (streamable HTTP, no key needed; the anonymous rate limit applies). Add that URL to any MCP client. For a local install, `PYTHONPATH=src python -m tethera.mcp_server` runs the same server over stdio.
