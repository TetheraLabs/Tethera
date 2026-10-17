# Tethera API

A pre-trade crowding check for automated traders on Robinhood Chain. Two read-only endpoints, JSON, no authentication needed. The same signal is available as an MCP server for agents.

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
- `verdict`: `herd` (15+ automated wallets one way, the other side at most 15% of that), `busy` (15+ but two-sided, or above the 95th percentile), `clean`.

## GET /v1/herds

Every pool with a one-sided automated crowd in the last minute, biggest first.

| parameter | required | meaning |
|---|---|---|
| `min_wallets` | no | Crowd size threshold, 5 to 500. Default 15 |

```json
{"herds": [{"pool": "0x52e65B17…", "direction": "buy0", "auto_wallets_same": 23, "auto_wallets_opposite": 2, "as_of": "2026-10-17T20:51:03+00:00"}]}
```

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

The same three calls (`crowding`, `herds`, `health`) as MCP tools over stdio, with descriptions written for an agent deciding whether to trade. Client configuration, with `__REPO__` replaced by the checkout path:

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
