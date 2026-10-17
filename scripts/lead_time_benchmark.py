"""Lead-time benchmark: does Tethera's herd signal warn of a pool's price impact earlier than
the reactive toxic-flow detectors it competes with?

Detectors, each evaluated block by block per pool:
  herd15 / herd5   Tethera: distinct automated wallets on one side in the trailing 60 s >= N,
                   with the other side <= 15% of it (mirrors src/tethera/signal.py).
  imb60            Cumulative directional imbalance over the trailing 60 blocks (Anti-Toxicity-Hook style):
                   |buy - sell| / total >= 0.6 and total >= the pool's median non-empty 60-block volume.
  imb_ema          Same test on exponentially weighted buy/sell volume (half-life 30 blocks).
  vpin             VPIN: volume buckets of one mean-60-block volume each, order imbalance per bucket,
                   mean over the last 50 buckets; fires at or above the pool's 95th percentile.
Ground truth: an impact event is a block whose median swap price moved more than X vs the price 60
blocks earlier, confirmed at the next priced block (>= X/2 vs the same reference), with one event per
300-block episode. Data-driven thresholds (imbalance median, VPIN p95) are fitted on the first half of
the window and every detector is scored on events in the second half only.

Usage: python scripts/lead_time_benchmark.py [--from TS] [--to TS] [--table swaps] [--pools 40]
                                             [--pool ADDR ...] [--out reports/lead_time_benchmark.md]
"""
import os, sys, time, math
from collections import deque, defaultdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import psycopg

DB = os.environ.get("DATABASE_URL", "postgresql://localhost/tethera")


def arg(name, default, cast=str):
    return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


TABLE = arg("--table", "swaps")
TS_FROM = arg("--from", None)
TS_TO = arg("--to", None)
NPOOLS = arg("--pools", 40, int)
NHERD = arg("--herd-pools", 30, int)   # pools with the most one-sided herd minutes are added to the set
OUT = arg("--out", os.path.join(os.path.dirname(__file__), "..", "reports", "lead_time_benchmark.md"))
POOLS = [sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--pool"]

THRESHOLDS = (0.01, 0.005)
LOOKBACK = 60          # blocks for price reference and imbalance window
COOLDOWN = 300         # blocks; one event per episode
HORIZON = 600          # blocks before an event in which an alert counts as a warning
EPISODE_GAP = 60       # alert blocks closer than this belong to one alert episode
VPIN_BUCKETS = 50
EMA_HALFLIFE = 30.0
REL_MULT = 3.0         # an event must also be this many times the pool's median absolute 60-block move

log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)


def window_bounds(conn):
    lo, hi = TS_FROM, TS_TO
    if lo is None or hi is None:
        row = conn.execute(f"SELECT min(block_time), max(block_time) FROM {TABLE}").fetchone()
        lo = lo or row[0]; hi = hi or row[1]
    return pd.Timestamp(lo), pd.Timestamp(hi)


def candidate_pools(conn, lo, hi):
    if POOLS:
        return POOLS
    rows = conn.execute(f"""SELECT pool, count(*) FROM {TABLE} WHERE block_time >= %s AND block_time <= %s
                            GROUP BY pool ORDER BY 2 DESC LIMIT %s""", (lo, hi, NPOOLS)).fetchall()
    pools = [r[0] for r in rows]
    # plus the pools where one-sided herds actually happened, from the per-minute aggregates, so the herd
    # signal is scored where it fires and not only on the busiest (mostly two-sided) pools
    herd = conn.execute("""
        WITH m AS (SELECT pool, minute, direction, auto_wallets FROM pool_minutes WHERE minute >= %s AND minute <= %s),
             p AS (SELECT a.pool, a.auto_wallets AS same, coalesce(b.auto_wallets, 0) AS opp
                   FROM m a LEFT JOIN m b ON b.pool = a.pool AND b.minute = a.minute AND b.direction <> a.direction)
        SELECT pool, count(*) FROM p WHERE same >= 15 AND opp <= 0.15 * same GROUP BY pool ORDER BY 2 DESC LIMIT %s""",
        (lo, hi, NHERD)).fetchall()
    for pool, _ in herd:
        if pool not in pools:
            pools.append(pool)
    return pools


def load_pool(conn, pool, lo, hi):
    q = f"""SELECT block_number, extract(epoch FROM block_time)::float8 AS t, log_index, direction,
                   tx_from, coalesce(is_automated, false) AS auto,
                   abs(amount0)::float8 AS a0, abs(amount1)::float8 AS a1
            FROM {TABLE} WHERE pool = %s AND block_time >= %s AND block_time <= %s
            ORDER BY block_number, log_index"""
    cur = conn.execute(q, (pool, lo, hi))
    cols = [c.name for c in cur.description]
    return pd.DataFrame(cur.fetchall(), columns=cols)


# ---------------------------------------------------------------- ground truth

def impact_events(df, x, mid_block=None):
    """Blocks whose median price moved > x vs the last price >= 60 blocks earlier, confirmed at the next priced block.
    The move must also exceed REL_MULT times the pool's median absolute 60-block move (fitted before mid_block), so
    pools that jitter more than x on every swap do not count every swap as an impact."""
    priced = df[(df.a0 > 0) & (df.a1 > 0)]
    if priced.empty:
        return np.array([], dtype=np.int64), np.array([]), np.array([])
    pb = priced.groupby("block_number", sort=True)
    blocks = pb.size().index.to_numpy()
    price = (priced.a1 / priced.a0).groupby(priced.block_number).median().to_numpy()
    ref_idx = np.searchsorted(blocks, blocks - LOOKBACK, side="right") - 1
    ok = ref_idx >= 0
    move = np.full(len(blocks), np.nan)
    move[ok] = price[ok] / price[ref_idx[ok]] - 1.0
    calib = np.abs(move[ok & ((blocks < mid_block) if mid_block is not None else True)])
    floor = max(x, REL_MULT * (np.median(calib) if len(calib) else 0.0))
    events = []
    last = -10 ** 9
    for i in range(len(blocks)):
        if not ok[i] or abs(move[i]) <= floor or blocks[i] - last < COOLDOWN:
            continue
        # confirmation: next priced block within LOOKBACK still shows at least half the move vs the same reference
        if i + 1 < len(blocks) and blocks[i + 1] - blocks[i] <= LOOKBACK:
            nxt = price[i + 1] / price[ref_idx[i]] - 1.0
            if abs(nxt) < floor / 2 or np.sign(nxt) != np.sign(move[i]):
                continue
        events.append(blocks[i]); last = blocks[i]
    return np.array(events, dtype=np.int64), blocks, price


# ---------------------------------------------------------------- detectors
# Each returns a sorted numpy array of alert block numbers (blocks with swaps where the condition held).

def herd_alerts(df, n_min):
    auto = df[df.auto]
    if auto.empty:
        return np.array([], dtype=np.int64)
    t = auto.t.to_numpy(); d = auto.direction.to_numpy(); w = auto.tx_from.to_numpy(); b = auto.block_number.to_numpy()
    win = deque(); cnt = {"sell0": defaultdict(int), "buy0": defaultdict(int)}
    alerts = []
    n = len(auto); i = 0
    while i < n:
        blk = b[i]; j = i
        while j < n and b[j] == blk:
            win.append((t[j], d[j], w[j])); cnt[d[j]][w[j]] += 1; j += 1
        cutoff = t[i] - 60.0
        while win and win[0][0] <= cutoff:
            _, dd, ww = win.popleft()
            c = cnt[dd]; c[ww] -= 1
            if c[ww] == 0:
                del c[ww]
        s, bb = len(cnt["sell0"]), len(cnt["buy0"])
        same, opp = max(s, bb), min(s, bb)
        if same >= n_min and opp <= 0.15 * same:
            alerts.append(blk)
        i = j
    return np.array(alerts, dtype=np.int64)


def per_block_volumes(df):
    sign = np.where(df.direction.to_numpy() == "buy0", 1.0, -1.0)
    vol = df.a1.to_numpy()
    g = pd.DataFrame({"b": df.block_number.to_numpy(), "buy": np.where(sign > 0, vol, 0.0), "sell": np.where(sign < 0, vol, 0.0)}).groupby("b").sum()
    return g.index.to_numpy(), g.buy.to_numpy(), g.sell.to_numpy()


def imbalance_alerts(df, mid_block):
    blocks, buy, sell = per_block_volumes(df)
    if len(blocks) == 0:
        return np.array([], dtype=np.int64), np.array([], dtype=np.int64)
    bmin, bmax = blocks[0], blocks[-1]
    size = int(bmax - bmin + 1)
    dense_b = np.zeros(size); dense_s = np.zeros(size)
    dense_b[blocks - bmin] = buy; dense_s[blocks - bmin] = sell
    cb = np.concatenate([[0.0], np.cumsum(dense_b)]); cs = np.concatenate([[0.0], np.cumsum(dense_s)])
    off = blocks - bmin
    lo = np.maximum(off - LOOKBACK + 1, 0)
    wb = cb[off + 1] - cb[lo]; ws = cs[off + 1] - cs[lo]
    tot = wb + ws; imb = np.abs(wb - ws)
    calib = tot[(blocks < mid_block) & (tot > 0)]
    med = np.median(calib) if len(calib) else np.inf
    fire = (tot > 0) & (imb / np.where(tot > 0, tot, 1) >= 0.6) & (tot >= med)
    # EMA version: decay by block gap
    k = math.log(2) / EMA_HALFLIFE
    eb = np.zeros(len(blocks)); es = np.zeros(len(blocks))
    b_acc = s_acc = 0.0; prev = blocks[0]
    for i in range(len(blocks)):
        decay = math.exp(-k * (blocks[i] - prev)); prev = blocks[i]
        b_acc = b_acc * decay + buy[i]; s_acc = s_acc * decay + sell[i]
        eb[i] = b_acc; es[i] = s_acc
    etot = eb + es
    ecal = etot[(blocks < mid_block) & (etot > 0)]
    emed = np.median(ecal) if len(ecal) else np.inf
    efire = (etot > 0) & (np.abs(eb - es) / np.where(etot > 0, etot, 1) >= 0.6) & (etot >= emed)
    return blocks[fire], blocks[efire]


def vpin_alerts(df, mid_block):
    blocks, buy, sell = per_block_volumes(df)
    if len(blocks) < 10:
        return np.array([], dtype=np.int64), float("nan")
    span = blocks[-1] - blocks[0] + 1
    V = (buy.sum() + sell.sum()) / span * LOOKBACK        # mean volume per 60 blocks
    if V <= 0:
        return np.array([], dtype=np.int64), float("nan")
    # fill buckets of size V, splitting a block's volume across buckets pro rata
    bucket_oi = []; bucket_end = []
    fill = 0.0; bb = 0.0; bs = 0.0
    for i in range(len(blocks)):
        b_rem, s_rem = buy[i], sell[i]
        while b_rem + s_rem > 0:
            room = V - fill
            take = min(room, b_rem + s_rem)
            frac = take / (b_rem + s_rem)
            bb += b_rem * frac; bs += s_rem * frac; fill += take
            b_rem -= b_rem * frac; s_rem -= s_rem * frac
            if fill >= V - 1e-9:
                bucket_oi.append(abs(bb - bs) / V); bucket_end.append(blocks[i])
                fill = bb = bs = 0.0
            if take <= 0:
                break
    if len(bucket_oi) < VPIN_BUCKETS + 5:
        return np.array([], dtype=np.int64), float("nan")
    oi = np.array(bucket_oi); ends = np.array(bucket_end)
    vpin = pd.Series(oi).rolling(VPIN_BUCKETS).mean().to_numpy()
    calib = vpin[(ends < mid_block) & ~np.isnan(vpin)]
    if len(calib) == 0:
        return np.array([], dtype=np.int64), float("nan")
    thr = np.percentile(calib, 95)
    # per block: VPIN of the last completed bucket at or before that block
    idx = np.searchsorted(ends, blocks, side="right") - 1
    ok = idx >= 0
    v = np.full(len(blocks), np.nan); v[ok] = vpin[idx[ok]]
    fire = ok & ~np.isnan(v) & (v >= thr)
    return blocks[fire], float(thr)


# ---------------------------------------------------------------- scoring

def post_move(blocks, price, starts, horizon=HORIZON):
    """Max |relative price move| within `horizon` blocks after each start block (price at start = last priced block <= start)."""
    out = []
    for st in starts:
        i0 = np.searchsorted(blocks, st, side="right") - 1
        i1 = np.searchsorted(blocks, st + horizon, side="right")
        if i0 < 0 or i1 <= i0 + 1:
            continue
        p0 = price[i0]
        out.append(float(np.max(np.abs(price[i0 + 1:i1] / p0 - 1.0))))
    return out


def score(events, alerts):
    """Per event: lead in blocks (earliest alert in [E-HORIZON, E-1]) or None, and the lead from the start of the
    latest alert episode that began inside that window. Plus alert episodes and false alarms."""
    leads = []; ep_leads = []
    starts = np.array([], dtype=np.int64)
    if len(alerts):
        starts = np.array([alerts[0]] + [alerts[i] for i in range(1, len(alerts)) if alerts[i] - alerts[i - 1] > EPISODE_GAP])
    for e in events:
        i = np.searchsorted(alerts, e - HORIZON, side="left")
        if i < len(alerts) and alerts[i] <= e - 1:
            leads.append(int(e - alerts[i]))
            j = np.searchsorted(starts, e - 1, side="right") - 1
            ep_leads.append(int(e - starts[j]) if j >= 0 and e - starts[j] <= HORIZON else None)
        else:
            leads.append(None); ep_leads.append(None)
    episodes = 0; false = 0
    if len(alerts):
        starts = list(starts) + [alerts[i] for i in range(1, len(alerts)) if alerts[i] - alerts[i - 1] > EPISODE_GAP]
        episodes = len(starts)
        for s in starts:
            j = np.searchsorted(events, s, side="left")
            if not (j < len(events) and events[j] - s <= HORIZON):
                false += 1
    return leads, ep_leads, episodes, false


def main():
    t0 = time.time()
    conn = psycopg.connect(DB)
    conn.execute("SET statement_timeout = '600s'")
    lo, hi = window_bounds(conn)
    mid_ts = lo + (hi - lo) / 2
    pools = candidate_pools(conn, lo, hi)
    log(f"window {lo} -> {hi}, calibration before {mid_ts}, {len(pools)} pools, table {TABLE}")

    results = {x: {d: {"events": 0, "hits": 0, "leads": [], "ep_leads": [], "episodes": 0, "false": 0, "duty": [], "swap_blocks": 0} for d in DETECTORS} for x in THRESHOLDS}
    moves = {d: [] for d in DETECTORS}; moves["baseline"] = []
    matched = {d: {"n": 0, "above_med": 0, "above_p90": 0} for d in DETECTORS}   # within-pool comparison
    per_pool = []
    for n, pool in enumerate(pools, 1):
        t1 = time.time()
        df = load_pool(conn, pool, lo, hi)
        if len(df) < 500:
            continue
        # mid block: first block at or after the midpoint timestamp
        mid_block = int(df.block_number[df.t >= mid_ts.timestamp()].min()) if (df.t >= mid_ts.timestamp()).any() else int(df.block_number.max())
        alerts = {}
        alerts["herd15"] = herd_alerts(df, 15)
        alerts["herd5"] = herd_alerts(df, 5)
        alerts["imb60"], alerts["imb_ema"] = imbalance_alerts(df, mid_block)
        alerts["vpin"], vthr = vpin_alerts(df, mid_block)
        n_swap_blocks = int(df.block_number[df.block_number >= mid_block].nunique())
        row = {"pool": pool, "swaps": len(df), "auto_share": round(float(df.auto.mean()), 3), "vpin_p95": vthr}
        _, pblocks, pprice = impact_events(df, THRESHOLDS[0], mid_block)
        if len(pblocks):
            sec = pblocks[pblocks >= mid_block]
            base = post_move(pblocks, pprice, sec[::max(1, len(sec) // 400)])
            moves["baseline"] += base
            bmed = np.median(base) if base else None; bp90 = np.percentile(base, 90) if base else None
            for d in DETECTORS:
                a = alerts[d][alerts[d] >= mid_block]
                if len(a):
                    starts = np.array([a[0]] + [a[i] for i in range(1, len(a)) if a[i] - a[i - 1] > EPISODE_GAP])
                    pm = post_move(pblocks, pprice, starts)
                    moves[d] += pm
                    if bmed is not None and pm:
                        matched[d]["n"] += len(pm)
                        matched[d]["above_med"] += int(np.sum(np.array(pm) > bmed))
                        matched[d]["above_p90"] += int(np.sum(np.array(pm) > bp90))
        for x in THRESHOLDS:
            ev, _, _ = impact_events(df, x, mid_block)
            ev = ev[ev >= mid_block]            # score on the second half only
            row[f"events_{x}"] = len(ev)
            for d in DETECTORS:
                a = alerts[d][alerts[d] >= mid_block - HORIZON]
                leads, ep_leads, eps, fls = score(ev, a)
                r = results[x][d]
                r["events"] += len(ev); r["hits"] += sum(l is not None for l in leads)
                r["leads"] += [l for l in leads if l is not None]; r["ep_leads"] += [l for l in ep_leads if l is not None]
                r["episodes"] += eps; r["false"] += fls
                r["duty"].append(int((alerts[d] >= mid_block).sum())); r["swap_blocks"] += n_swap_blocks
                row[f"{d}_hits_{x}"] = sum(l is not None for l in leads)
                row[f"{d}_fa_{x}"] = (fls, eps)
                if x == THRESHOLDS[0]:
                    row[f"{d}_median_lead"] = float(np.median([l for l in leads if l is not None])) if any(l is not None for l in leads) else None
                    row[f"{d}_fa"] = f"{fls}/{eps}"
        per_pool.append(row)
        log(f"{n}/{len(pools)} {pool[:10]} {len(df)} swaps, events 1%={row['events_0.01']} 0.5%={row['events_0.005']}, {time.time()-t1:.1f}s")
    conn.close()
    write_report(results, per_pool, lo, hi, mid_ts, pools, time.time() - t0, moves, matched)


DETECTORS = ("herd15", "herd5", "imb60", "imb_ema", "vpin")
LABELS = {"herd15": "Tethera herd (>=15 automated wallets, one-sided)", "herd5": "Tethera herd, loose (>=5)",
          "imb60": "Directional imbalance, 60-block window", "imb_ema": "Directional imbalance, EMA (half-life 30 blocks)",
          "vpin": "VPIN (50 buckets, p95 threshold)"}


def write_report(results, per_pool, lo, hi, mid_ts, pools, secs, moves, matched):
    L = []
    L.append("# Lead-time benchmark: herd signal vs reactive toxic-flow detectors\n")
    L.append(f"Window {lo:%Y-%m-%d %H:%M} to {hi:%Y-%m-%d %H:%M} UTC on table `{TABLE}`; "
             f"thresholds fitted before {mid_ts:%Y-%m-%d %H:%M}, scored after it. {len(per_pool)} pools (top {NPOOLS} by swap count). "
             f"Run time {secs/60:.1f} min.\n")
    L.append("## Method\n")
    L.append("- **Impact event:** a block whose median swap price moved more than X against the last price at least 60 blocks earlier, "
             "confirmed at the next priced block (at least half the move the same way), at most one event per 300 blocks. X = 1% and 0.5%. "
             f"The move must also be at least {REL_MULT:g}x the pool's median absolute 60-block move, so pools that jitter by more than X on every swap do not count every swap as an impact.")
    L.append("- **Warning:** the earliest alert block in the 600 blocks (about 60 s) before the event. Lead = event block minus that block. "
             "Because a detector that is always on scores a 600-block lead by construction, the table also gives the lead from the start of the latest alert episode before the event, and the alert duty cycle (share of blocks with swaps that were on alert).")
    L.append("- **False alarm:** an alert episode (alert blocks closer than 60 blocks are one episode) with no event in the 600 blocks after it starts.")
    L.append("- **Detectors:** " + "; ".join(f"`{d}` {LABELS[d]}" for d in DETECTORS) + ".")
    L.append("- Data-driven thresholds (imbalance volume median, VPIN 95th percentile) come from the first half of the window; all detectors are scored on second-half events only, so the comparison is like for like.")
    L.append("- Stock-token pools were not identified separately: `pools` holds token addresses only and the RPC needed to resolve symbols was unavailable.\n")
    for x in THRESHOLDS:
        L.append(f"## Results, impact threshold {x*100:g}%\n")
        L.append("| detector | events | hits | hit rate | median lead (earliest alert) | median lead (episode start) | alert duty cycle | alert episodes | false alarms |")
        L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for d in DETECTORS:
            r = results[x][d]
            ev = r["events"]; hr = r["hits"] / ev if ev else float("nan")
            med = np.median(r["leads"]) if r["leads"] else float("nan"); mean = np.mean(r["leads"]) if r["leads"] else float("nan")
            fa = r["false"] / r["episodes"] if r["episodes"] else float("nan")
            epm = np.median(r["ep_leads"]) if r["ep_leads"] else float("nan")
            duty = sum(r["duty"]) / r["swap_blocks"] if r["swap_blocks"] else float("nan")
            L.append(f"| `{d}` | {ev} | {r['hits']} | {hr:.0%} | {med:.0f} blocks ({med/10:.1f} s) | {epm:.0f} blocks ({epm/10:.1f} s) | {duty:.0%} | {r['episodes']} | {fa:.0%} |")
        L.append("")
        L.append(f"Macro average over pools with at least 5 events at {x*100:g}% (each pool weighted equally, so busy jittery pools do not dominate):\n")
        L.append("| detector | pools | mean hit rate | mean false-alarm rate |")
        L.append("|---|---:|---:|---:|")
        for d in DETECTORS:
            hrs = []; fas = []
            for r in per_pool:
                if r[f"events_{x}"] >= 5:
                    hrs.append(r[f"{d}_hits_{x}"] / r[f"events_{x}"])
                    f, e = r[f"{d}_fa_{x}"]
                    if e:
                        fas.append(f / e)
            L.append(f"| `{d}` | {len(hrs)} | {np.mean(hrs) if hrs else float('nan'):.0%} | {np.mean(fas) if fas else float('nan'):.0%} |")
        L.append("")
    L.append("## Price move after an alert episode starts\n")
    L.append("Largest absolute price move in the 600 blocks after each alert episode starts, against a baseline of evenly spaced blocks in the same pools. "
             "This asks the prior question: does the thing each detector flags move the price at all?\n")
    L.append("| series | episodes | median max move | p75 | p90 | share > 1% |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for d in ("baseline",) + DETECTORS:
        m = np.array(moves[d])
        if len(m) == 0:
            L.append(f"| `{d}` | 0 | - | - | - | - |"); continue
        L.append(f"| `{d}` | {len(m)} | {np.median(m):.2%} | {np.percentile(m, 75):.2%} | {np.percentile(m, 90):.2%} | {(m > 0.01).mean():.0%} |")
    L.append("")
    L.append("Same test matched within each pool (the baseline median and 90th percentile are the pool's own). With no effect, 50% of episodes would sit above the pool median and 10% above its p90.\n")
    L.append("| detector | episodes | above pool median | above pool p90 |")
    L.append("|---|---:|---:|---:|")
    for d in DETECTORS:
        m = matched[d]
        if m["n"]:
            L.append(f"| `{d}` | {m['n']} | {m['above_med']/m['n']:.0%} | {m['above_p90']/m['n']:.0%} |")
    L.append("")
    L.append("## Per pool (1% events)\n")
    L.append("| pool | swaps | auto share | events 1% | events 0.5% | herd15 hits / med lead / FA | herd5 | imb60 | imb_ema | vpin |")
    L.append("|---|---:|---:|---:|---:|---|---|---|---|---|")
    for r in per_pool:
        def cell(d):
            ml = r.get(f"{d}_median_lead"); ml = "-" if ml is None else f"{ml:.0f}"
            return f"{r[f'{d}_hits_0.01']} / {ml} / {r[f'{d}_fa']}"
        L.append(f"| `{r['pool'][:10]}…` | {r['swaps']} | {r['auto_share']:.0%} | {r['events_0.01']} | {r['events_0.005']} | "
                 + " | ".join(cell(d) for d in DETECTORS) + " |")
    L.append("")
    L.append("## Interpretation\n")
    L.append("_(filled in by hand after the run)_\n")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        f.write("\n".join(L))
    log(f"wrote {OUT}")


if __name__ == "__main__":
    main()
