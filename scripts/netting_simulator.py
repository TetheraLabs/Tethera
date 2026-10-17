"""Netting simulator: replay the recorded UniswapX orders and measure what matching opposing orders
against each other (instead of each going to the AMM through a filler) would have been worth.

For every filled Dutch order we know its creation time, its decay curve, when and at what price it
actually settled, and whether a cosigner gave one filler exclusivity. The sweep walks time in one-second
steps; whenever two orders on opposite sides of the same token pair are open at once, the overlapping
amount is matched at the mid of their two current Dutch limit prices. Each side's improvement is how
much better that mid is than the price it really got. Whatever is left unmatched is assumed routed
through the pair's busiest pool at that minute's median executed price.

Three scenarios, because the order lifetime decides everything:
  actual    an order is open from creation until its real fill (usually under a second)
  extended  an order is open for its whole Dutch life (creation to the end of decay, ~12 s)
  respected extended, but an order with an exclusive filler only becomes available to anyone else
            when its exclusivity ends (the decay start block)

Usage: python scripts/netting_simulator.py [--from 2026-10-14] [--to 2026-10-18] [--out reports/netting_simulator.md]
Reads uniswapx_orders, uniswapx_fills, pools, pool_minutes, tokens. Nothing else.
"""
import os, sys, time, math, bisect
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import psycopg

def arg(name, default): return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default
DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")
FROM = datetime.fromisoformat(arg("--from", "2026-10-14")).replace(tzinfo=timezone.utc)
TO = datetime.fromisoformat(arg("--to", "2026-10-18")).replace(tzinfo=timezone.utc)
OUT = arg("--out", os.path.join(os.path.dirname(__file__), "..", "reports", "netting_simulator.md"))
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)

ZERO = "0x0000000000000000000000000000000000000000"
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
BLOCKS_PER_S = 10.0
HERD = 15

def norm(t): return WETH if t.lower() == ZERO else t

class Order:
    __slots__ = ("h", "pair", "side", "A", "B", "created", "fill_ts", "fill_block", "decay_start", "decay_blocks", "exclusive",
                 "in_amt", "out_start", "out_min", "settled_in", "settled_out", "settled_px", "t_open", "t_close", "t_avail",
                 "rem", "matched_qty", "matched_value", "net_gain", "amm_gain", "amm_px", "herd", "minute", "pool", "filler_is_exclusive")

def dutch_out(o, t):
    """Output amount the order offers at time t (seconds), following its linear decay."""
    b = o.fill_block + (t - o.fill_ts) * BLOCKS_PER_S
    if b <= o.decay_start or o.decay_blocks <= 0: return o.out_start
    k = min(1.0, (b - o.decay_start) / o.decay_blocks)
    return o.out_start - k * (o.out_start - o.out_min)

def limit_px(o, t):
    """Order's current limit price in B per A: an A-seller's ask, a B-seller's (A-buyer's) bid."""
    out = dutch_out(o, t)
    if o.side == "sellA": return out / o.in_amt                        # receives B per A sold
    return o.in_amt / out if out > 0 else float("inf")                 # pays B per A bought

def load(conn):
    rows = conn.execute("""
        SELECT o.order_hash, o.input_token, o.output_token, o.raw, f.filler
        FROM uniswapx_orders o LEFT JOIN uniswapx_fills f ON f.order_hash = o.order_hash
        WHERE o.last_status = 'filled' AND o.created_at >= %s AND o.created_at < %s AND o.raw ? 'settledAmounts'""", (FROM, TO)).fetchall()
    orders = []; skipped = defaultdict(int)
    for h, tin, tout, raw, filler in rows:
        try:
            cd = raw.get("cosignerData") or {}
            outs = [x for x in raw["outputs"] if norm(x["token"]) == norm(tout)]
            settled = raw.get("settledAmounts") or []
            s_in = sum(int(s["amountIn"]) for s in settled if norm(s["tokenIn"]) == norm(tin))
            s_out = sum(int(s["amountOut"]) for s in settled if norm(s["tokenOut"]) == norm(tout))
            if not (s_in and s_out and outs and raw.get("fillBlock") and raw.get("fillTimestamp")): skipped["incomplete"] += 1; continue
            o = Order(); o.h = h; o.A, o.B = sorted([norm(tin), norm(tout)]); o.pair = (o.A, o.B)
            o.side = "sellA" if norm(tin) == o.A else "sellB"
            o.created = float(raw["createdAt"]); o.fill_ts = float(raw["fillTimestamp"]); o.fill_block = int(raw["fillBlock"])
            o.decay_start = int(cd.get("decayStartBlock") or o.fill_block)
            curve = outs[0].get("curve") or {}; rb = curve.get("relativeBlocks") or [0]; o.decay_blocks = int(rb[-1])
            overrides = cd.get("outputOverrides") or []
            start = sum(int(x["startAmount"]) for x in outs)
            if overrides and any(int(v) for v in overrides):
                # overrides are per output in raw['outputs'] order; take those for our token
                idx = [i for i, x in enumerate(raw["outputs"]) if norm(x["token"]) == norm(tout)]
                start = sum(int(overrides[i]) for i in idx if i < len(overrides)) or start
            o.out_start = float(start); o.out_min = float(sum(int(x["minAmount"]) for x in outs))
            o.in_amt = float(int(raw["input"]["startAmount"]))
            o.settled_in, o.settled_out = float(s_in), float(s_out)
            o.settled_px = (o.settled_out / o.settled_in) if o.side == "sellA" else (o.settled_in / o.settled_out)   # B per A
            ex = cd.get("exclusiveFiller"); o.exclusive = bool(ex) and int(ex, 16) != 0
            o.filler_is_exclusive = bool(filler and ex and filler.lower() == ex.lower())
            o.t_open = o.created
            o.t_close = max(o.fill_ts, o.created + 1e-3)
            o.t_avail = o.fill_ts + (o.decay_start - o.fill_block) / BLOCKS_PER_S if o.exclusive else o.created
            o.minute = datetime.fromtimestamp(o.created, timezone.utc).replace(second=0, microsecond=0)
            orders.append(o)
        except Exception as e:
            skipped[type(e).__name__] += 1
    log(f"{len(orders):,} filled orders loaded, skipped {dict(skipped)}")
    return orders

def pool_prices(conn, pairs):
    """For each pair: busiest pool and its per-minute median executed price (B per A) by direction, plus herd flags."""
    out = {}
    for A, B in pairs:
        pools = conn.execute("""SELECT p.pool, p.token0 = %s AS a_is_0, coalesce((SELECT sum(swaps) FROM pool_minutes m WHERE m.pool = p.pool AND m.minute >= %s AND m.minute < %s), 0) AS n
                                FROM pools p WHERE (p.token0 = %s AND p.token1 = %s) OR (p.token0 = %s AND p.token1 = %s) ORDER BY n DESC LIMIT 1""",
                             (A, FROM, TO, A, B, B, A)).fetchone()
        if not pools or pools[2] == 0: continue
        pool, a_is_0, _ = pools
        rows = conn.execute("SELECT minute, direction, px_median::float8, auto_wallets FROM pool_minutes WHERE pool = %s AND minute >= %s AND minute < %s AND px_median > 0",
                            (pool, FROM - timedelta(minutes=15), TO)).fetchall()
        series = defaultdict(dict)   # minute -> {direction: (px in B per A, auto_wallets)}
        for minute, d, px, aw in rows:
            px_ba = px if a_is_0 else (1.0 / px if px else None)     # px_median is token1 per token0
            series[minute][d] = (px_ba, aw)
        out[(A, B)] = {"pool": pool, "a_is_0": a_is_0, "series": series, "minutes": sorted(series)}
    return out

def amm_lookup(pp, o):
    """Pool median price (B per A) for the order's side in its minute, or the latest within 10 minutes before."""
    if pp is None: return None, None, False
    d = ("sell0" if o.side == "sellA" else "buy0") if pp["a_is_0"] else ("buy0" if o.side == "sellA" else "sell0")
    opp = "buy0" if d == "sell0" else "sell0"
    i = bisect.bisect_right(pp["minutes"], o.minute) - 1
    while i >= 0:
        m = pp["minutes"][i]
        if (o.minute - m) > timedelta(minutes=10): break
        row = pp["series"][m]
        if d in row:
            same = row[d][1]; other = row[opp][1] if opp in row else 0
            return row[d][0], m, (same >= HERD and other <= 0.15 * same)
        i -= 1
    return None, None, False

def sweep(orders, window):
    """Match opposing open orders second by second. window: 'actual' | 'extended' | 'respected'."""
    for o in orders:
        o.rem = o.in_amt; o.matched_qty = 0.0; o.matched_value = 0.0; o.net_gain = 0.0
    by_pair = defaultdict(list)
    for o in orders: by_pair[o.pair].append(o)
    for pair, os_ in by_pair.items():
        if len({o.side for o in os_}) < 2: continue
        def span(o):
            if window == "actual": return o.t_open, o.t_close
            end = o.fill_ts + (o.decay_start + o.decay_blocks - o.fill_block) / BLOCKS_PER_S
            start = o.t_avail if window == "respected" else o.t_open
            return start, max(end, start + 1e-3)
        spans = {o.h: span(o) for o in os_}
        t0 = math.floor(min(s[0] for s in spans.values())); t1 = math.ceil(max(s[1] for s in spans.values()))
        events = sorted(os_, key=lambda o: spans[o.h][0]); ei = 0; open_ = []
        t = t0
        while t <= t1:
            while ei < len(events) and spans[events[ei].h][0] <= t: open_.append(events[ei]); ei += 1
            open_ = [o for o in open_ if spans[o.h][1] >= t and o.rem > 1e-9 * o.in_amt]
            asks = sorted((o for o in open_ if o.side == "sellA"), key=lambda o: limit_px(o, t))
            bids = sorted((o for o in open_ if o.side == "sellB"), key=lambda o: -limit_px(o, t))
            while asks and bids:
                a, b = asks[0], bids[0]; pa, pb = limit_px(a, t), limit_px(b, t)
                if not (pa <= pb) or pb == float("inf"): break
                mid = 0.5 * (pa + pb)
                qa = a.rem; qb = b.rem / mid                      # b.rem is in B; A-equivalent at the mid
                q = min(qa, qb)
                if q <= 0: break
                a.rem -= q; b.rem -= q * mid
                a.matched_qty += q; b.matched_qty += q
                a.matched_value += q * mid; b.matched_value += q * mid
                a.net_gain += q * (mid - a.settled_px)            # seller gets more B per A than it did
                b.net_gain += q * (b.settled_px - mid)            # buyer pays fewer B per A than it did
                if a.rem <= 1e-9 * a.in_amt: asks.pop(0)
                if b.rem <= 1e-9 * b.in_amt: bids.pop(0)
            if not open_ and ei < len(events): t = max(t + 1, math.floor(spans[events[ei].h][0]))
            else: t += 1
    return orders

def main():
    t0 = time.time()
    with psycopg.connect(DB) as conn:
        orders = load(conn)
        pairs = sorted({o.pair for o in orders})
        pp = pool_prices(conn, pairs); log(f"{len(pairs)} pairs, {len(pp)} with a pool price series")
        decimals = dict(conn.execute("SELECT token, decimals FROM tokens WHERE decimals IS NOT NULL").fetchall())
        symbols = dict(conn.execute("SELECT token, coalesce(symbol, left(token, 8)) FROM tokens").fetchall())
        eth = conn.execute("""SELECT minute, px_median::float8 FROM pool_minutes WHERE pool = (
                                SELECT p.pool FROM pools p WHERE p.token0 = %s AND p.token1 = %s
                                ORDER BY (SELECT sum(swaps) FROM pool_minutes m WHERE m.pool = p.pool) DESC NULLS LAST LIMIT 1)
                              AND direction = 'sell0' AND px_median > 0 ORDER BY minute""", (WETH, USDG)).fetchall()
    eth_minutes = [m for m, _ in eth]; eth_px = [px * 1e12 for _, px in eth]      # USDG(6) per WETH(18) -> USD per ETH
    eth_median = sorted(eth_px)[len(eth_px) // 2] if eth_px else None
    def eth_usd(minute):
        if not eth_px: return None
        i = bisect.bisect_right(eth_minutes, minute) - 1
        return eth_px[i] if i >= 0 else eth_median
    sym = lambda t: symbols.get(t, t[:8])
    def usd_of_B(o, amount_B):
        """Value an amount of token B (raw units) in USD, via USDG or ETH on either leg; None if neither."""
        if o.B == USDG: return amount_B / 1e6
        if o.B == WETH: e = eth_usd(o.minute); return amount_B / 1e18 * e if e else None
        if o.A == USDG: return amount_B / o.settled_px / 1e6
        if o.A == WETH: e = eth_usd(o.minute); return amount_B / o.settled_px / 1e18 * e if e else None
        return None

    # exclusivity facts
    n = len(orders); excl = sum(o.exclusive for o in orders); pre = sum(o.fill_block < o.decay_start for o in orders)
    byex = sum(o.filler_is_exclusive for o in orders)
    excl_blocks = sorted(o.decay_start - o.fill_block for o in orders if o.exclusive)
    life = sorted(o.fill_ts - o.created for o in orders)
    med = lambda xs: xs[len(xs) // 2] if xs else float("nan")
    days = max((max(o.created for o in orders) - min(o.created for o in orders)) / 86400, 1e-6)   # span of the orders actually seen
    # the filler's spread benchmark: settled vs the pool's median executed price in that minute, no netting
    amm_rows = []
    for o in orders:
        px, m, herd = amm_lookup(pp.get(o.pair), o)
        o.amm_px, o.herd = px, herd
        if px:
            gain_B = o.in_amt * (px - o.settled_px) if o.side == "sellA" else o.in_amt * (o.settled_px - px) / o.settled_px
            value_B = o.in_amt * o.settled_px if o.side == "sellA" else o.in_amt
            amm_rows.append((o, gain_B, value_B))
    results = {}
    for window in ("actual", "extended", "respected"):
        sweep(orders, window)
        matched = [o for o in orders if o.matched_qty > 0]
        vol_usd = 0.0; net_usd = 0.0; unvalued = 0; bps = []; herd_net = 0.0; calm_net = 0.0; herd_vol = 0.0; calm_vol = 0.0
        per_pair = defaultdict(lambda: [0.0, 0.0, 0])
        for o in orders:
            value_B = o.in_amt * o.settled_px if o.side == "sellA" else o.in_amt
            v = usd_of_B(o, value_B)
            if v is None: unvalued += 1; continue
            vol_usd += v
            if o.matched_qty > 0:
                mv = usd_of_B(o, o.matched_value); g = usd_of_B(o, o.net_gain)
                if mv is None or g is None: continue
                net_usd += g; per_pair[o.pair][0] += mv; per_pair[o.pair][1] += g; per_pair[o.pair][2] += 1
                bps.append(1e4 * o.net_gain / o.matched_value)
                if o.herd: herd_net += g; herd_vol += mv
                else: calm_net += g; calm_vol += mv
        matched_usd = sum(v[0] for v in per_pair.values())
        results[window] = dict(matched_orders=len(matched), orders=len(orders), vol_usd=vol_usd, matched_usd=matched_usd, net_usd=net_usd,
                               bp_median=med(sorted(bps)), bp_mean=(sum(bps) / len(bps) if bps else float("nan")),
                               bp_weighted=(1e4 * net_usd / matched_usd if matched_usd else float("nan")),
                               herd=(herd_net, herd_vol), calm=(calm_net, calm_vol), unvalued=unvalued,
                               per_pair=sorted(per_pair.items(), key=lambda kv: -kv[1][0]))
        log(f"{window}: {len(matched)} of {len(orders)} orders matched, ${matched_usd:,.0f} of ${vol_usd:,.0f} nettable, improvement ${net_usd:,.0f}")

    amm_usd = 0.0; amm_vol = 0.0; amm_bps = []; amm_n = 0; amm_herd = [0.0, 0.0]; amm_calm = [0.0, 0.0]
    for o, g, v in amm_rows:
        gu = usd_of_B(o, g); vu = usd_of_B(o, v)
        if gu is None or vu is None: continue
        amm_usd += gu; amm_vol += vu; amm_n += 1; amm_bps.append(1e4 * g / v)
        (amm_herd if o.herd else amm_calm)[0] += gu; (amm_herd if o.herd else amm_calm)[1] += vu
    no_px = sum(1 for o in orders if o.amm_px is None)

    R = results["extended"]
    L = [f"# Netting simulator: UniswapX orders on Robinhood Chain, {FROM:%-d %b} to {TO:%-d %b %Y}\n",
         f"{n:,} filled Dutch V3 orders replayed, {len(pairs)} token pairs, {len(pp)} pairs with a pool price series. "
         f"USD values use USDG at par and the WETH/USDG pool's per-minute median price (median ${eth_median:,.0f}/ETH); "
         f"orders with neither USDG nor ETH on either leg are counted but not valued ({R['unvalued']} of them).\n",
         "## First, can an outside filler fill these orders at all?\n",
         f"- {excl / n:.0%} of orders carry an exclusive filler set by the cosigner. {byex / n:.0%} were filled by exactly that filler.",
         f"- {pre / n:.0%} were filled *before* their decay started, i.e. inside the exclusivity window, at the cosigner's override price.",
         f"- Median time from order creation to fill: {med(life):.1f} s (90th percentile {life[int(0.9 * len(life))]:.1f} s). Median exclusivity: {med(excl_blocks):.0f} blocks (about {med(excl_blocks) / BLOCKS_PER_S:.1f} s) from fill to decay start.",
         "- Plainly: the UniswapX flow on this chain is routed by the cosigner to a named filler and settled within a second. An outside filler, Tethera included, "
         "only ever sees the orders that the exclusive filler declines, which is the remaining few percent. Netting as a public filler is therefore not available on today's flow; "
         "it would need either a cosigner arrangement with Uniswap or Tethera's own order flow.\n",
         "## What netting would have been worth\n",
         "| scenario | orders matched | nettable volume | share of volume | improvement | weighted bp | median bp per order |\n|---|---:|---:|---:|---:|---:|---:|"]
    for w, label in (("actual", "actual lifetimes (creation to real fill)"), ("extended", "full Dutch life (~12 s), exclusivity ignored"), ("respected", "full Dutch life, exclusivity respected")):
        r = results[w]
        L.append(f"| {label} | {r['matched_orders']:,} / {r['orders']:,} | ${r['matched_usd']:,.0f} | {r['matched_usd'] / r['vol_usd'] if r['vol_usd'] else 0:.1%} | ${r['net_usd']:,.0f} | {r['bp_weighted']:.1f} | {r['bp_median']:.1f} |")
    L.append(f"\nTotal valued order volume: ${R['vol_usd']:,.0f} over the {days:.1f} days of orders seen (${R['vol_usd'] / days:,.0f}/day).")
    for w in ("extended", "respected"):
        r = results[w]
        L.append(f"- {w}: improvement ${r['net_usd']:,.0f} in total = ${r['net_usd'] / days:,.0f}/day; a 20% share is ${0.2 * r['net_usd'] / days:,.0f}/day, 30% is ${0.3 * r['net_usd'] / days:,.0f}/day.")
    hn, hv = R["herd"]; cn, cv = R["calm"]
    L.append(f"\n**Herd minutes vs calm minutes (extended scenario).** Herd = {HERD}+ automated wallets one way in the matched pool that minute, with the other side at most 15% of it. "
             f"Netted volume in herd minutes ${hv:,.0f} with improvement ${hn:,.0f} ({1e4 * hn / hv if hv else float('nan'):.1f} bp); in calm minutes ${cv:,.0f} with ${cn:,.0f} ({1e4 * cn / cv if cv else float('nan'):.1f} bp).")
    L.append("\n### By pair (extended scenario, top 10 by nettable volume)\n\n| pair | orders matched | nettable volume | improvement | bp |\n|---|---:|---:|---:|---:|")
    top3 = 0.0
    for i, (pair, (mv, g, k)) in enumerate(R["per_pair"][:10]):
        if i < 3: top3 += mv
        L.append(f"| {sym(pair[0])}/{sym(pair[1])} | {k} | ${mv:,.0f} | ${g:,.0f} | {1e4 * g / mv if mv else float('nan'):.1f} |")
    L.append(f"\nThe three busiest pairs hold {top3 / R['matched_usd'] if R['matched_usd'] else 0:.0%} of all nettable volume.")
    L.append("\n## The remainder: what the fillers kept\n")
    L.append(f"Every order not matched goes to the AMM. Comparing each order's settled price with its pool's median executed price in the same minute "
             f"(no netting at all, {amm_n:,} orders with a pool price; {no_px} orders had no pool price within 10 minutes and are left out): "
             f"the swapper would have been better off by ${amm_usd:,.0f} on ${amm_vol:,.0f} of volume, {1e4 * amm_usd / amm_vol if amm_vol else float('nan'):.1f} bp weighted, median {med(sorted(amm_bps)):.1f} bp per order. "
             f"That is the spread between the Dutch settlement and the pool's own mid-minute price, which is roughly what the filler earned. "
             f"Herd minutes: {1e4 * amm_herd[0] / amm_herd[1] if amm_herd[1] else float('nan'):.1f} bp on ${amm_herd[1]:,.0f}; calm minutes: {1e4 * amm_calm[0] / amm_calm[1] if amm_calm[1] else float('nan'):.1f} bp on ${amm_calm[1]:,.0f}.\n")
    L.append("## Assumptions and what they do to the numbers\n")
    L += ["- Matching happens at the mid of the two limit prices with no gas, no inventory and no slippage; this is the best case for netting.",
          "- An order's limit price follows its cosigned decay curve (override start, linear to the minimum over the stated blocks). Time to block uses 10 blocks/s from the fill block.",
          "- The 'extended' scenarios assume an order stays available for its whole Dutch life. On today's flow it does not: it is filled within a second by the exclusive filler. "
          "The 'actual' row is what a public filler could really have netted, and it is the honest number.",
          "- Pool median prices come from the busiest pool for the pair in that minute; those are executed prices including other traders' slippage, so the remainder comparison is realistic, not a quoted mid.",
          "- USD values only exist where a leg is USDG or ETH. Pairs like memecoin/memecoin are matched but not valued.",
          "- Three and a half days of orders, about one a minute. The counts are small; treat the bp figures as indicative."]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT, "w").write("\n".join(L) + "\n")
    log(f"wrote {OUT} in {time.time() - t0:.0f}s")

if __name__ == "__main__":
    main()
