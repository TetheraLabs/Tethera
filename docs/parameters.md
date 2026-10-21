# Strategy parameter dictionary

Every parameter the desk's search loops can change, or will be able to, grouped by the concept it comes from: classical technical analysis, quantitative factors, auction theory, risk and execution, Wyckoff, on-chain microstructure, smart money concepts and the ICT vocabulary.
Each entry gives the name, what the parameter does, the range the loops search within, and whether it is
**searched** today or **proposed** (defined here, wired into a strategy next). Values are never listed: they are
what the search finds, and they stay private. Families are added as we find them.

How a parameter is used matters more than its name, so each family notes where it enters the strategy: a
**gate** is a condition a trade must pass, a **composite** is a running score inside a gate, a **bonus** adds to a
composite, a **weight** scores a candidate level, a **rule** changes sizing or exit.

Status key: searched = in the loops today; proposed = defined, not yet in a strategy; reading = a feature the
strategy reads, listed so the weights on it make sense.

---

## Part I. Traditional technical analysis

Indicators with their standard parameters, each usable as a gate, a bias reading or an exit rule. These are the
building blocks the desk's own strategies use and the evolutionary loop can combine.

### 1.1 Moving averages

- `MA_FAST`, `MA_SLOW` — periods of the fast and slow averages for a crossover. Ranges 5 to 50 and 20 to 300. Searched in the desk baselines.
- `MA_TYPE` — simple, exponential or volume-weighted. Proposed.
- `MA_SLOPE_MIN` — minimum slope of the slow average, in ATR per bar, for a trend to count. Range 0 to 0.2. Proposed.

### 1.2 MACD (Appel)

- `MACD_FAST`, `MACD_SLOW`, `MACD_SIGNAL` — the 12, 26, 9 convention. Ranges 5 to 20, 15 to 50, 3 to 15. Proposed.
- `MACD_HIST_MIN` — minimum histogram magnitude, in ATR, for a signal. Range 0 to 0.5. Proposed.

### 1.3 RSI (Wilder)

- `RSI_N` — period. Range 5 to 30. Searched in the desk baselines.
- `RSI_LO`, `RSI_HI` — oversold and overbought bounds. Ranges 15 to 40 and 60 to 85. Searched in the desk baselines.
- `RSI_EXIT_BAND` — the band around 50 where the position is flattened. Range 0 to 15. Searched in the desk baselines.
- `RSI_DIVERGENCE_LOOKBACK` — bars over which price and RSI divergence is detected. Range 10 to 60. Proposed.

### 1.4 Bollinger bands and z-scores

- `BB_N`, `BB_K` — period and width in standard deviations, 20 and 2 by convention. Ranges 10 to 60 and 1 to 3. Proposed.
- `BB_SQUEEZE_PCT` — band width below this percentile of recent widths flags a squeeze. Range 0.05 to 0.3. Proposed.
- `ZSCORE_ENTRY`, `ZSCORE_EXIT` — entry and exit thresholds for mean reversion on the z-score of price to its average. Ranges 1 to 3 and 0 to 1. Proposed.

### 1.5 Donchian channels and breakouts

- `BREAKOUT_N` — lookback for the channel; a close beyond it enters. Range 10 to 200. Searched in the desk baselines.
- `BREAKOUT_EXIT_N` — a shorter channel for the exit (Turtle rules used 20 in, 10 out). Range 5 to 100. Proposed.
- `BREAKOUT_CONFIRM_BARS` — bars price must hold beyond the channel. Range 0 to 3. Proposed.

### 1.6 Average true range and volatility

- `ATR_N` — period, 14 by convention. Range 5 to 50. Proposed.
- `ATR_STOP_MULT` — stop as a multiple of ATR. Range 0.5 to 4. Proposed (see the premium and discount subsection2 for the gate chain's version).
- `ATR_TRAIL_MULT` — trailing stop as a multiple of ATR (the chandelier exit). Range 1 to 5. Proposed.
- `VOL_PCTL_MIN`, `VOL_PCTL_MAX` — trade only when realised volatility sits within this percentile band of its history. Ranges 0 to 0.5 and 0.5 to 1. Proposed.

### 1.7 ADX and trend strength (Wilder)

- `ADX_N` — period. Range 7 to 30. Proposed.
- `ADX_MIN_TREND` — minimum ADX for trend rules to be active. Range 15 to 35. Proposed.
- `ADX_MAX_RANGE` — maximum ADX for range rules to be active. Range 15 to 30. Proposed.

### 1.8 Stochastic oscillator (Lane)

- `STOCH_K`, `STOCH_D`, `STOCH_SMOOTH` — the 14, 3, 3 convention. Ranges 5 to 30, 2 to 10, 1 to 5. Proposed.
- `STOCH_LO`, `STOCH_HI` — bounds. Ranges 10 to 30 and 70 to 90. Proposed.

### 1.9 VWAP and its bands

- `VWAP_ANCHOR` — session, week or a chosen swing. Proposed.
- `VWAP_BAND_K` — band width in standard deviations of price around VWAP. Range 0.5 to 3. Proposed.
- `VWAP_REVERT_Z`, `VWAP_REVERT_N` — fade stretches beyond this many standard deviations over this window. Ranges 1 to 3 and 20 to 120. Searched in the desk baselines (chain bars carry a true VWAP from fills).

### 1.10 Pivot points and classical levels

- `PIVOT_METHOD` — floor, Camarilla, Woodie or Fibonacci pivots. Proposed.
- `PIVOT_TOL_ATR` — tolerance for price to count as at a pivot. Range 0.05 to 0.5. Proposed.
- `ROUND_NUMBER_STEP` — spacing of round-number levels used as targets or stops. Proposed.

### 1.11 Candlestick patterns

- `PATTERN_SET` — which reversal and continuation patterns are read (engulfing, pin bar, inside bar, doji). Proposed.
- `PATTERN_BODY_RATIO_MIN` — minimum body-to-range ratio for a pattern to count. Range 0.3 to 0.8. Proposed.

### 1.12 Fibonacci retracement and extension

- `FIB_RETRACE_LEVELS` — which retracements are read (0.382, 0.5, 0.618, 0.786). Proposed.
- `FIB_EXT_TARGET` — extension used as a target (1.272, 1.618). Range 1.1 to 2. Proposed.
- `FIB_SWING_LOOKBACK` — bars defining the swing the levels are drawn from. Range 10 to 200. Proposed.

### 1.13 Turtle Soup (Raschke and Connors)

- `FVG_TURTLE_SOUP_BONUS` — added when the gap follows a false breakout of a recent extreme. Range 0 to 3. Bonus. Searched.
- `FVG_TURTLE_SOUP_REVERSAL_BONUS` — added again when a reversal candle confirms it. Range 0 to 3. Bonus. Searched.
- `TS_LOOKBACK` — the lookback whose extreme is broken, 20 in the original. Range 10 to 40. Proposed.
- `TS_MIN_DAYS_SINCE_EXTREME` — the prior extreme must be at least this old, 3 in the original. Range 1 to 5. Proposed.

---

---

## Part II. Quantitative and academic factors

### 2.1 Momentum (Jegadeesh and Titman; time-series momentum)

- `MOM_LOOKBACK` — the formation period, in bars or days. Range 20 to 250. Proposed.
- `MOM_SKIP` — the most recent bars skipped to avoid short-term reversal (the 12-minus-1 convention). Range 0 to 25. Proposed.
- `TSMOM_VOL_SCALE` — size the position inversely to realised volatility (Moskowitz, Ooi and Pedersen). Rule. Proposed.

### 2.2 Short-term reversal and mean reversion

- `MR_HALF_LIFE_MAX` — trade mean reversion only when the fitted half-life of deviations is below this many bars. Range 5 to 200. Proposed.
- `MR_ENTRY_Z`, `MR_EXIT_Z` — entry and exit on the deviation z-score. Ranges 1 to 3 and 0 to 1. Proposed (see the Bollinger subsection).

### 2.3 Intraday seasonality

- `OVERNIGHT_ONLY` — hold only across the close-to-open, where most of the equity premium has historically accrued. Rule. Proposed.
- `FIRST_N_MIN_EXCLUDE`, `LAST_N_MIN_EXCLUDE` — skip the opening and closing minutes. Ranges 0 to 60. Proposed.
- `LUNCH_EXCLUDE` — skip 11:30 to 13:30 New York. Gate. Proposed (see the kill zones subsection).
- `CLOSE_AUCTION_ALIGN` — align with the direction of the last thirty minutes' flow. Rule. Proposed.

### 2.4 Calendar effects

- `TURN_OF_MONTH_DAYS` — days around the month end treated as a long bias window. Range 1 to 4. Proposed.
- `FOMC_PRE_DRIFT` — long bias in the session before scheduled policy announcements. Rule. Proposed.
- `OPEX_WEEK_FLAG` — options expiry week treated differently. Gate. Proposed.
- `DOW_BIAS` — day-of-week biases (see the weekly profiles subsection for the gate chain's version). Proposed.

### 2.5 Volatility regimes

- `REALVOL_N` — window for realised volatility. Range 10 to 100. Proposed.
- `REGIME_VOL_SPLIT` — the percentile that splits calm from stressed regimes, with separate parameter sets per regime. Range 0.5 to 0.9. Proposed.
- `GARCH_FORECAST_HORIZON` — bars ahead a fitted volatility model forecasts for sizing. Range 1 to 20. Proposed.

### 2.6 Order flow and informed trading

- `OFI_N` — window for order-flow imbalance (buys minus sells). Range 5 to 60. Proposed.
- `OFI_ENTRY_Z` — imbalance z-score that triggers an entry in its direction. Range 1 to 3. Proposed.
- `VPIN_BUCKETS` — volume buckets for volume-synchronised probability of informed trading (Easley, Lopez de Prado and O'Hara). Range 20 to 100. Proposed.
- `VPIN_MAX` — stand aside when VPIN exceeds this. Range 0.5 to 0.9. Proposed.
- `KYLE_LAMBDA_N` — window for the price-impact coefficient; large impact means thin liquidity. Range 20 to 200. Proposed.

---

---

## Part III. Market Profile and auction theory (Steidlmayer, Dalton)

### 3.1 Opening types (Dalton's day character)

The day's opening is classified as open-drive, open-test-drive, open-rejection-reverse or open-auction, and the
bias threshold is set separately for each, because conviction at the open differs by type.

- `BIAS_THRESHOLD_OPEN_DRIVE_UP`, `BIAS_THRESHOLD_OPEN_DRIVE_DOWN` — thresholds on open-drive days, by direction. Range 0.5 to 6. Gate. Searched.
- `BIAS_THRESHOLD_OPEN_TEST_UP`, `BIAS_THRESHOLD_OPEN_TEST_DOWN` — on open-test-drive days. Range 0.5 to 6. Gate. Searched.
- `BIAS_THRESHOLD_OPEN_REJECTION_UP`, `BIAS_THRESHOLD_OPEN_REJECTION_DOWN` — on open-rejection-reverse days. Range 0.5 to 6. Gate. Searched.
- `BIAS_THRESHOLD_OPEN_AUCTION` — on open-auction days. Range 0.5 to 6. Gate. Searched.

### 3.2 Value area and point of control

- `TGT_VP_PD_POC_W` — target weight on the previous day's point of control. Range 0 to 10. Weight. Searched.
- `TGT_VP_PD_VAH_W`, `TGT_VP_PD_VAL_W` — previous day's value-area high and low. Range 0 to 10. Weight. Searched.
- `VA_PCT` — the share of volume that defines the value area (70% is Dalton's convention). Range 0.6 to 0.8. Proposed.
- `VA_80_RULE_ENABLED` — the 80% rule: an open outside value that re-enters and holds two periods targets the other side of value. Gate. Proposed.
- `POC_MIGRATION_LOOKBACK` — days over which point-of-control migration sets a trend reading. Range 2 to 10. Proposed.

### 3.3 Initial balance and range extension

- `TGT_VP_IB_HIGH_W`, `TGT_VP_IB_LOW_W` — target weights on today's initial balance high and low. Range 0 to 10. Weight. Searched.
- `IB_MINUTES` — length of the initial balance, 60 by convention. Range 30 to 90. Proposed.
- `IB_EXT_TRIGGER` — range extension counts once price exceeds the balance by this fraction of its width. Range 0.1 to 1. Proposed.
- `IB_EXT_TARGET_MULT` — target as a multiple of the balance width beyond the break (1.0, 1.5, 2.0 in profile practice). Range 0.5 to 2.5. Proposed.
- `IB_NARROW_PCT` — a balance narrower than this percentile of recent balances flags a likely trend day. Range 0.1 to 0.4. Proposed.

---

---

## Part IV. Risk, sizing and execution

### 4.1 Sizing

- `VOL_TARGET_ANNUAL` — annualised volatility the position is sized to. Range 0.05 to 0.4. Proposed.
- `KELLY_FRACTION` — fraction of the Kelly-optimal size used. Range 0.1 to 0.5. Proposed.
- `MAX_POSITION` — cap on position as a fraction of equity. Range 0.5 to 2. Proposed.

### 4.2 Drawdown control

- `DD_THROTTLE_START`, `DD_THROTTLE_FLOOR` — reduce size linearly from this drawdown to this size floor. Ranges 0.02 to 0.1 and 0.1 to 0.5. Proposed.
- `LOSS_STREAK_PAUSE` — pause after this many consecutive losses. Range 2 to 6. Proposed.
- `DAILY_LOSS_STOP_PCT` — stop trading for the day after this loss. Range 0.005 to 0.03. Proposed.

### 4.3 Exits

- `TIME_STOP_BARS` — exit after this many bars if neither stop nor target is hit. Range 5 to 200. Proposed.
- `TRAIL_ACTIVATE_R`, `TRAIL_DISTANCE_R` — start trailing after this many R of profit, at this distance. Ranges 0.5 to 2 and 0.5 to 2. Proposed.

### 4.4 Execution on chain

- `COST_BPS_ASSUMED` — the one-way cost the strategy is optimised against (the desk evaluates at 5, 15 and 40). Range 5 to 40. Fixed by the scorer.
- `ORDER_SPLIT_N` — number of child orders an entry is split into. Range 1 to 10. Proposed.
- `PARTICIPATION_MAX` — maximum share of a minute's traded volume the strategy will be. Range 0.01 to 0.2. Proposed.
- `POOL_FEE_MAX_BPS` — highest pool fee tier the router will use. Range 1 to 30. Proposed.
- `ROUTE_SPLIT_MIN_DEPTH_USD` — minimum pool depth before a pool receives a share of the order. Proposed.

---

---

## Part V. Wyckoff method

- `WYCKOFF_PHASE_LOOKBACK` — bars over which accumulation or distribution structure is read. Range 50 to 400. Proposed.
- `SPRING_MAX_BARS` — a spring (a dip below the range low that recovers) must recover within this many bars. Range 1 to 5. Proposed.
- `UPTHRUST_MAX_BARS` — the mirror for distribution. Range 1 to 5. Proposed.
- `EFFORT_RESULT_RATIO` — volume relative to the move it produced; a large effort with a small result flags absorption. Range 1 to 4. Proposed.
- `SOS_VOLUME_MULT` — a sign of strength needs volume at this multiple of average. Range 1.2 to 3. Proposed.

---

---

## Part VI. Chain-native microstructure (unique to on-chain data)

Readings built from the chain's own fills and wallet labels. No off-chain venue has them.

- `AUTO_SHARE_MAX` — stand aside when more than this share of the minute's fills came from automated wallets. Range 0.3 to 0.9. Proposed.
- `HERD_FADE_ENABLED`, `HERD_MIN_WALLETS` — fade a one-sided herd of at least this many automated wallets. Range 10 to 50. Proposed.
- `SELF_CANCEL_SHARE_MAX` — stand aside when printed, self-cancelling volume exceeds this share. Range 0.2 to 0.8. Proposed.
- `FLEET_ENTRY_PENALTY` — reduce size when wallets flagged as launch fleets are active in the pool. Range 0 to 1. Proposed.
- `WALLET_AGE_MIX_MIN` — minimum share of fills from wallets older than thirty days. Range 0 to 0.8. Proposed.
- `SWAPS_PER_MIN_MIN` — minimum fills per minute for a bar to be tradeable. Range 1 to 20. Proposed.
- `CALM_WAIT_MAX_S` — seconds to wait for a herd to clear before filling. Range 0 to 120. Proposed.

---

## Part VII. Smart Money Concepts (SMC)

SMC is a retail-popular reorganisation of ICT's vocabulary around structure. Where a concept already exists above
it is cross-referenced; the entries here are the parameters SMC adds.

### 7.1 Structure: break of structure and change of character

- `BOS_SWING_LOOKBACK` — bars each side that define a swing point. Range 2 to 10. Proposed.
- `BOS_CONFIRM_CLOSE` — a break counts on a close beyond the swing (on) or on a wick (off). Gate. Proposed.
- `CHOCH_REQUIRED` — a change of character against the prior trend is required before counter-trend entries. Gate. Proposed.
- `STRUCTURE_TF_MULT` — the higher timeframe used for structure, as a multiple of the entry timeframe. Range 3 to 12. Proposed.

### 7.2 Order blocks and breaker blocks

- `OB_MAX_AGE` — oldest order block, in bars, that may be used. Range 10 to 200. Proposed.
- `OB_MIN_DISPLACEMENT_ATR` — the move away from the block must be at least this many ATR to qualify it. Range 0.5 to 3. Proposed.
- `OB_ENTRY_DEPTH` — how far into the block price must trade for an entry, as a fraction of its height. Range 0 to 1. Proposed.
- `OB_MAX_MITIGATIONS` — a block is retired after this many touches. Range 1 to 3. Proposed.
- `BREAKER_ENABLED` — a failed order block that price breaks through becomes a breaker in the other direction. Gate. Proposed.

### 7.3 Inducement and liquidity grabs

- `INDUCEMENT_REQUIRED` — a minor pool must be taken before the major one for the sweep to count. Gate. Proposed.
- `GRAB_MAX_BARS_BEYOND` — a grab must return inside the level within this many bars. Range 1 to 5. Proposed.
- `EQUAL_LEVEL_TOL_ATR` — tolerance for two highs or lows to count as equal. Range 0.05 to 0.3. Proposed.

### 7.4 Imbalance, premium and discount

- Fair value gaps and premium and discount: see the ICT subsections below.
- `OTE_FIB_LOW`, `OTE_FIB_HIGH` — the optimal trade entry retracement band, 0.62 to 0.79 by convention. Ranges 0.5 to 0.7 and 0.7 to 0.9. Proposed.

---

---

## Part VIII. ICT concepts: the gate chain searched today

The strategy is a chain: a daily bias decides direction; an entry then needs a liquidity sweep, displacement
and a fair value gap in that direction, inside an allowed time window, with a target chosen from weighted
levels and a stop from structure. The vocabulary is ICT's (Inner Circle Trader, Michael Huddleston).

### 8.1 Draw on liquidity and daily bias

The bias is a weighted sum of directional readings, each in −1 to +1, each weight searched in −2 to +2. The sum
must clear a threshold to allow longs (positive) or shorts (negative).

- `BIAS_WEIGHTS[dol_direction]` — weight on the current draw on liquidity: which resting pool (an old high or low) price is expected to reach next. Range −2 to 2. Searched.
- `BIAS_WEIGHTS[dol_weekly_bias]` — the same at weekly scale. Range −2 to 2. Searched.
- `BIAS_WEIGHTS[daily_bias_direction]` — weight on the daily candle's structural direction. Range −2 to 2. Searched.
- `BIAS_WEIGHTS[pdh_intact]`, `BIAS_WEIGHTS[pdl_intact]` — weight on whether the previous day's high or low still stands, each an unclaimed pool. Range −2 to 2. Searched.
- `BIAS_THRESHOLD` — minimum absolute bias sum before any trade. Range 0.5 to 6. Gate. Searched.
- `BIAS_SCALE_LONG`, `BIAS_SCALE_SHORT` — multipliers on the sum before the threshold test, one per side. Gate. Searched indirectly (set by the proposer).
- `STRICT_BIAS_GATE` — when on, the bias is a hard condition rather than a score. Gate. Searched.

### 8.2 Premium, discount and IPDA data ranges

- `BIAS_WEIGHTS[ipda_20d_position]`, `[ipda_40d_position]`, `[ipda_60d_position]` — weight on where price sits in the 20, 40 and 60 day ranges; discount favours longs, premium favours shorts. Range −2 to 2. Searched.
- `FVG_DR_ALIGNMENT_BONUS` — added to a gap's score when it sits on the right side of the dealing range. Range 0 to 3. Bonus. Searched.
- `TGT_IPDA_Q25_W`, `TGT_IPDA_Q50_W`, `TGT_IPDA_Q75_W` — target weights on the quarter, half and three-quarter levels of the 20-day range. Range 0 to 10. Weight. Searched.
- `STRICT_REGIME_FILTER` — when on, drops trades not aligned with both the daily and the dealing-range regime. Gate. Searched.

### 8.3 Midnight open, opening ranges and the midnight opening range

- `BIAS_WEIGHTS[midnight_open_bias]` — weight on price above or below the midnight New York open. Range −2 to 2. Searched.
- `BIAS_WEIGHTS[mor_direction]` — weight on the direction of the break from the midnight opening range. Range −2 to 2. Searched.
- `BIAS_WEIGHTS[or_break_direction]` — weight on the direction of the break from the cash opening range. Range −2 to 2. Searched.
- `TGT_DAILY_OPEN_W` — target weight on the daily open. Range 0 to 10. Weight. Searched.

### 8.4 Opening gaps: new day, new week

- `BIAS_WEIGHTS[ndog_quad_bias]`, `BIAS_WEIGHTS[nwog_quad_bias]` — weight on price's quadrant relative to the new-day and new-week opening gaps. Range −2 to 2. Searched.
- `TGT_NDOG_HIGH_W`, `TGT_NDOG_LOW_W`, `TGT_NDOG_MID_W` — target weights on the edges and midpoint of the new-day opening gap. Range 0 to 10. Weight. Searched.
- `TGT_NWOG_HIGH_W`, `TGT_NWOG_LOW_W`, `TGT_NWOG_MID_W` — the same for the new-week opening gap. Range 0 to 10. Weight. Searched.

### 8.5 Power of Three: accumulation, manipulation, distribution

- `BIAS_WEIGHTS[pot_true_direction]` — weight on the true direction implied by the day's accumulation and manipulation phases anchored at midnight. Range −2 to 2. Searched.
- `BIAS_WEIGHTS[pot830_true_direction]` — the same anchored at 08:30 New York. Range −2 to 2. Searched.
- `BIAS_WEIGHTS[potpm_true_direction]` — the same anchored at the afternoon session. Range −2 to 2. Searched.

### 8.6 Daily wick quadrants and PD arrays

- `BIAS_WEIGHTS[dwq_direction]` — weight on which quadrant of the previous day's selected wick price is in. Range −2 to 2. Searched.
- `FVG_HP_BONUS` — added when a gap overlaps the active wick quadrant zone, ICT's high-probability condition. Range 0 to 5. Bonus. Searched.

### 8.7 Liquidity sweeps

- `SWEEP_MAX_AGE` — how many bars ago the sweep may have happened. Range 2 to 15. Gate. Searched.
- `SWEEP_MIN_SCORE` — minimum sweep composite. Range 0.5 to 10. Gate. Searched.
- `SWEEP_DEPTH_BONUS_PER_BPS` — added per basis point the sweep went beyond the level. Range 0 to 0.5. Bonus. Searched.
- `STRICT_SWEEP_QUAL_MIN` — hard minimum on the sweep's quality when strict gates are on. Range 0 to 5. Gate. Searched.
- `STRICT_SWEEP_DISP_SEQ` — when on, displacement must follow the sweep. Gate. Searched.
- `KZ_POOL_IMBALANCE_BONUS` — added when resting liquidity is lopsided to one side. Range 0 to 3. Bonus. Searched.

### 8.8 Judas swing

- `SWEEP_JUDAS_BONUS` — added when the sweep is the day's false move in the opening window, then rejected. Range 0 to 5. Bonus. Searched.

### 8.9 SMT divergence (index symmetry)

- `SWEEP_SMT_BONUS` — added when the correlated index did not make the same sweep. Range 0 to 3. Bonus. Searched.
- `BIAS_WEIGHTS[index_symmetry_score]` — weight on agreement between the two index proxies. Range −2 to 2. Searched.

### 8.10 Displacement and market structure shift

- `DISP_MIN_SCORE` — minimum displacement composite. Range 0.1 to 5. Gate. Searched.
- `DISP_BODY_BONUS` — added for large candle bodies relative to range. Range 0 to 2. Bonus. Searched.
- `DISP_CONSEC_BONUS` — added per consecutive bar in the move, capped at three. Range 0 to 1. Bonus. Searched.
- `DISP_FVG_QUALITY_BONUS` — added for the quality of the gap the move leaves. Range 0 to 2. Bonus. Searched.
- `STRICT_DISP_MAG_MIN` — hard minimum on displacement magnitude when strict gates are on. Range 0 to 4. Gate. Searched.
- `STRICT_MSS_CONFIRM` — when on, a market structure shift in the trade's direction is required. Gate. Searched.

### 8.11 Fair value gaps

- `FVG_MAX_AGE` — oldest gap, in bars, that may be used. Range 4 to 30. Gate. Searched.
- `FVG_MIN_SCORE` — minimum gap composite. Range 0 to 5. Gate. Searched.
- `FVG_MAX_VISITS` — a gap is rejected after this many returns into it. Range 1 to 5. Gate. Searched.
- `FVG_PROXIMITY_BONUS` — added the closer price is to the gap. Range 0 to 3. Bonus. Searched.
- `FVG_FIRST_POST_SWEEP_BONUS` — added when this is the first gap after the sweep. Range 0 to 3. Bonus. Searched.
- `FVG_DECAY_BONUS` — added for a gap partially consumed but open. Range 0 to 2. Bonus. Searched.
- `FVG_UNFILLED_BONUS` — added in proportion to the unfilled share of the gap. Range 0 to 2. Bonus. Searched.
- `FVG_MIN_DISP_QUALITY` — minimum quality of the displacement that created the gap. Range 0 to 3. Gate. Searched.
- `STRICT_FVG_DISP_QUAL_MIN` — the hard version of the above when strict gates are on. Range 0 to 4. Gate. Searched.
- `FVG_IOFED_BONUS` — added when the institutional order flow entry drill conditions align. Range 0 to 3. Bonus. Searched.
- `FVG_TGIF_BONUS` — added for a Friday retracement into the week's range. Range 0 to 3. Bonus. Searched.

### 8.12 Mitigation blocks, breakers, reaper

- `FVG_MITIGATION_BONUS` — added when the gap overlaps a mitigation block. Range 0 to 3. Bonus. Searched.
- `FVG_REAPER_BONUS` — added for a momentum-exhaustion signal. Range 0 to 3. Bonus. Searched.
- `FVG_REAPER_MSB_BONUS` — added when a structure break confirms it. Range 0 to 2. Bonus. Searched.

### 8.13 Low-resistance liquidity runs

- `MAX_PATH_OBSTACLES` — reject if more than this many levels sit between entry and target. Range 2 to 10. Gate. Searched.
- `MAX_PATH_OPP_FVGS` — reject if more than this many opposing gaps sit on the path. Range 1 to 5. Gate. Searched.

### 8.14 Kill zones

- `KILL_ZONE_REQUIRED` — when on, entries only inside a kill zone. Gate. Searched.
- `KZ_MIN_SCORE` — minimum time composite. Range 0 to 3. Gate. Searched.
- `KZ_LONDON_BONUS` — 02:00 to 05:00 New York. Range 0 to 3. Bonus. Searched.
- `KZ_NY_OPEN_BONUS` — 07:00 to 09:00 New York. Range 0 to 3. Bonus. Searched.
- `KZ_LONDON_CLOSE_BONUS` — 10:00 to 12:00 New York. Range 0 to 3. Bonus. Searched.
- `KZ_NY_PM_BONUS` — 13:30 to 15:00 New York. Range 0 to 3. Bonus. Searched.
- `KZ_DEAD_ZONE_PENALTY` — subtracted in 05:00 to 07:00 and over lunch. Range 0 to 5. Penalty. Searched.

### 8.15 Macros and the Silver Bullet

- `KZ_MACRO_BONUS` — inside the twenty-minute windows around the hour. Range 0 to 3. Bonus. Searched.
- `KZ_SILVER_BULLET_BONUS` — inside the one-hour Silver Bullet windows. Range 0 to 3. Bonus. Searched.

### 8.16 Weekly profiles and day of week

- `KZ_DOW_TUE_BONUS` — Tuesday as the expansion day. Range 0 to 2. Bonus. Searched.
- `KZ_DOW_WED_BONUS` — Wednesday as the midweek reversal. Range 0 to 2. Bonus. Searched.
- `KZ_DOW_FRI_PENALTY` — Friday profit-taking. Range 0 to 2. Penalty. Searched.
- `KZ_WED_CONFIRMATION_BONUS` — Wednesday set the week's extreme and confirms direction. Range 0 to 3. Bonus. Searched.
- `KZ_WED_BREAKOUT_FAIL_PENALTY` — Wednesday crossed the weekly open and failed. Range 0 to 3. Penalty. Searched.
- `KZ_EXPANSION_DAY_BONUS` — today's range is the largest so far this week. Range 0 to 3. Bonus. Searched.
- `KZ_WEEKLY_OPEN_DISCOUNT_BONUS` — buying below the weekly open or selling above it. Range 0 to 3. Bonus. Searched.
- `TGT_PWH_W`, `TGT_PWL_W` — target weights on the previous week's high and low. Range 0 to 10. Weight. Searched.

### 8.17 Session liquidity as targets

- `TGT_PDH_W`, `TGT_PDL_W` — previous day's high and low. Range 0 to 10. Weight. Searched.
- `TGT_AM_SESSION_W` — morning session high and low as afternoon targets. Range 0 to 10. Weight. Searched.
- `TGT_LONDON_SESSION_W` — London high and low as New York targets. Range 0 to 10. Weight. Searched.
- `TGT_LUNCH_SESSION_W` — lunch high and low as afternoon targets. Range 0 to 10. Weight. Searched.
- `TGT_HTF_EQH_W`, `TGT_HTF_EQL_W` — higher-timeframe equal highs and lows. Range 0 to 10. Weight. Searched.
- `TGT_EQH_W`, `TGT_EQL_W` — intraday equal highs and lows. Range 0 to 10. Weight. Searched.
- `TGT_HTF_SWING_HIGH_W`, `TGT_HTF_SWING_LOW_W` — higher-timeframe swing points. Range 0 to 10. Weight. Searched.
- `TGT_D1_SWING_HIGH_W`, `TGT_D1_SWING_LOW_W` — daily swing points. Range 0 to 10. Weight. Searched.

### 8.18 Session structure and calendar

- `SESSION_FILTER` — which part of the day may trade: morning, afternoon, full, or kill zones only. Gate. Searched.
- `MACRO_WINDOW_FILTER` — when on, bars near scheduled economic releases are blacked out. Gate. Searched.
- `KZ_NFP_WEEK_PENALTY` — payrolls week. Range 0 to 3. Penalty. Searched.
- `KZ_PRE_CPI_PPI_BONUS` — the session before an inflation print. Range 0 to 3. Bonus. Searched.
- `KZ_POST_HOLIDAY_BONUS` — the session after a holiday. Range 0 to 3. Bonus. Searched.
- `EOD_FLAT_ENABLED`, `EOD_FLAT_MIN_ET`, `NO_ENTRY_MIN_ET` — flatten before the close, and stop entering some minutes before that. Rule. Searched.

### 8.19 Volatility cycles

- `KZ_RANGE_CONTRACTION_BONUS` — recent daily ranges have contracted. Range 0 to 3. Bonus. Searched.
- `KZ_RANGE_EXP_DUE_BONUS` — an expansion is statistically due. Range 0 to 3. Bonus. Searched.
- `KZ_DR_CONTRACTION_BONUS` — the dealing range has contracted. Range 0 to 3. Bonus. Searched.
- `LARGE_RANGE_MULT` — suppress first-hour entries when the previous day's range was more than this multiple of average. Range 1.5 to 3.5. Gate. Searched.

### 8.20 Trend alignment

- `BIAS_WEIGHTS[ema_stacking]` — weight on moving-average alignment, fast above slow being bullish. Range −2 to 2. Searched.
- `H4FVG_RISK_MULT` — size multiplier when the sweep happened inside a four-hour fair value gap. Rule. Searched.

### 8.21 Targets: confluence and reward-to-risk

- `CONFLUENCE_TOL_ATR` — two candidate levels within this many ATR of each other count as one stronger level. Range 0 to 0.5. Rule. Searched.
- `CONFLUENCE_MULT` — multiplier on a level's weight when it has confluence. Range 1 to 3. Rule. Searched.
- `MIN_RR` — minimum reward-to-risk of the chosen target against the stop. Range 0.5 to 3. Gate. Searched.

### 8.22 Stops, cooldown and partial exits

- `STOP_ATR_MULT` — stop distance as a multiple of average true range when no structural stop applies. Rule. Searched.
- `MIN_BARS_BETWEEN_TRADES` — cooldown in bars after a trade. Range 1 to 6. Rule. Searched.
- `PARTIAL_ENABLED` — take profit in parts. Rule. Searched.
- `PARTIAL_1_PCT`, `PARTIAL_1_SIZE` — first partial: at this share of the target distance, close this fraction. Ranges 0.15 to 0.45 and 0.05 to 0.5. Rule. Searched.
- `PARTIAL_2_PCT`, `PARTIAL_2_SIZE` — second partial, likewise. Ranges 0.35 to 0.7 and 0.1 to 0.75. Rule. Searched.
- `BE_TRIGGER_PCT`, `BE_OFFSET_PCT` — move the stop to break-even at this share of the target distance, offset by this fraction of price. Ranges 0.5 to 0.95 and 0 to 0.005. Rule. Searched.
- `TOD_RISK_MULT` — size multiplier by hour of day. Rule. Searched.
- `RISK_SCALE` — global multiplier on position size. Rule. Searched.
- `STRICT_EXEC_GATES` — turns the hard execution minimums on. Gate. Searched.

---

---

Account-specific parameters (daily loss caps or fixed risk in currency for a particular account) are not
searched by the desk and are not listed.
