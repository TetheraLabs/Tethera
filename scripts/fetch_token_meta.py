"""Fill the tokens table (symbol, name, decimals) for every token in pools that matter, via eth_call on
whichever RPC is up, then classify: stock (tokenised equities), stable, eth, other, copycat.

A symbol is cheap to copy (90 tokens call themselves SPCX, a dozen "USDC" are 18-decimal memecoins), so for the
stock, stable and eth kinds only one token per symbol keeps the kind: the one in the most pools, earliest deployed
on a tie. The rest become 'copycat' and no longer price anything. A stable must also sit in at least 20 pools: a lone
"USDC" with 18 decimals and three pools is not USDC, and one such token priced $400B of fake volume in a week.

Usage: python scripts/fetch_token_meta.py [--min-swaps 50]   (pools with at least this many swaps in `swaps`)
"""
import os, sys, time, re
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from dotenv import load_dotenv; load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
import psycopg
from tethera.rpc import live_rpc, RpcError

MIN = int(sys.argv[sys.argv.index("--min-swaps") + 1]) if "--min-swaps" in sys.argv else 50
rpc = live_rpc()
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)
SEL = {"symbol": "0x95d89b41", "name": "0x06fdde03", "decimals": "0x313ce567"}

def dec_string(hexdata):
    if not hexdata or hexdata == "0x": return None
    b = bytes.fromhex(hexdata[2:])
    if len(b) == 32:                       # bytes32 symbol (old tokens)
        return b.rstrip(b"\x00").decode("utf-8", "replace")
    if len(b) >= 64:
        off = int.from_bytes(b[0:32], "big"); ln = int.from_bytes(b[off:off+32], "big")
        return b[off+32:off+32+ln].decode("utf-8", "replace")
    return None

def classify(symbol, name):
    s = (symbol or "").upper(); n = (name or "").lower()
    if s in ("USDG", "USDC", "USDT", "DAI", "PYUSD", "USDE") or "usd" in n and "stable" in n: return "stable"
    if s in ("WETH", "ETH"): return "eth"
    if is_stock(symbol, name): return "stock"
    return "other"

def is_stock(symbol, name):
    """Tokenised equities and ETFs: xStocks (symbol like SPYx, AAPLx: upper-case ticker + lower-case x; name '... xStock'),
    Backpack/Backed securities tokens, and anything calling itself a stock or security. Case matters: MARSX, CNX are not stocks."""
    sym = symbol or ""; n = (name or "").lower()
    return bool(re.fullmatch(r"[A-Z]{1,6}x", sym) or re.search(r"xstock|securit(y|ies)|tokeni[sz]ed (stock|share|equity)|\bstock\b|\betf\b", n))

RECLASSIFY = r"""
UPDATE tokens SET kind = CASE
    WHEN symbol ~ '^[A-Z]{1,6}x$' OR lower(name) ~ '(xstock|securit(y|ies)|tokeni[sz]ed (stock|share|equity)|\mstock\M|\metf\M)' THEN 'stock'
    WHEN upper(symbol) IN ('USDG','USDC','USDT','DAI','PYUSD','USDE') THEN 'stable'
    WHEN upper(symbol) IN ('WETH','ETH') THEN 'eth'
    ELSE 'other' END
WHERE kind IS DISTINCT FROM 'copycat' AND kind IS DISTINCT FROM CASE
    WHEN symbol ~ '^[A-Z]{1,6}x$' OR lower(name) ~ '(xstock|securit(y|ies)|tokeni[sz]ed (stock|share|equity)|\mstock\M|\metf\M)' THEN 'stock'
    WHEN upper(symbol) IN ('USDG','USDC','USDT','DAI','PYUSD','USDE') THEN 'stable'
    WHEN upper(symbol) IN ('WETH','ETH') THEN 'eth'
    ELSE 'other' END;
"""

DEDUPE = """
WITH cand AS (
    SELECT t.token, t.kind, upper(t.symbol) sym,
           (SELECT count(*) FROM pools p WHERE p.token0 = t.token OR p.token1 = t.token) AS n_pools,
           (SELECT min(created_block) FROM pools p WHERE p.token0 = t.token OR p.token1 = t.token) AS first_block
    FROM tokens t WHERE t.kind IN ('stock', 'stable', 'eth', 'copycat') AND t.symbol IS NOT NULL
), ranked AS (
    SELECT token, n_pools, row_number() OVER (PARTITION BY sym ORDER BY n_pools DESC, first_block ASC NULLS LAST, token) AS rn FROM cand
)
UPDATE tokens t SET kind = CASE WHEN r.rn = 1 THEN CASE
        WHEN t.symbol ~ '^[A-Z]{1,6}x$' OR lower(t.name) ~ '(xstock|securit(y|ies)|tokeni[sz]ed (stock|share|equity)|\\mstock\\M|\\metf\\M)' THEN 'stock'
        WHEN upper(t.symbol) IN ('USDG','USDC','USDT','DAI','PYUSD','USDE') THEN CASE WHEN r.n_pools >= 20 THEN 'stable' ELSE 'copycat' END
        WHEN upper(t.symbol) IN ('WETH','ETH') THEN 'eth' ELSE 'other' END
    ELSE 'copycat' END
FROM ranked r WHERE r.token = t.token
  AND t.kind IS DISTINCT FROM CASE WHEN r.rn = 1 THEN CASE
        WHEN t.symbol ~ '^[A-Z]{1,6}x$' OR lower(t.name) ~ '(xstock|securit(y|ies)|tokeni[sz]ed (stock|share|equity)|\\mstock\\M|\\metf\\M)' THEN 'stock'
        WHEN upper(t.symbol) IN ('USDG','USDC','USDT','DAI','PYUSD','USDE') THEN CASE WHEN r.n_pools >= 20 THEN 'stable' ELSE 'copycat' END
        WHEN upper(t.symbol) IN ('WETH','ETH') THEN 'eth' ELSE 'other' END
    ELSE 'copycat' END;
"""

with psycopg.connect(os.environ.get("DATABASE_URL", "postgresql://localhost/tethera"), autocommit=True) as conn, conn.cursor() as cur:
    cur.execute("""
        WITH busy AS (SELECT pool FROM swaps GROUP BY pool HAVING count(*) >= %s)
        SELECT DISTINCT t FROM (SELECT p.token0 t FROM pools p JOIN busy USING (pool)
                                UNION SELECT p.token1 FROM pools p JOIN busy USING (pool)) x
        WHERE NOT EXISTS (SELECT 1 FROM tokens k WHERE k.token = x.t)""", (MIN,))
    todo = [r[0] for r in cur.fetchall()]; log(f"{len(todo)} tokens to fetch")
    for i, tok in enumerate(todo):
        row = {}
        try:
            res = rpc.batch([("eth_call", [{"to": tok, "data": d}, "latest"]) for d in SEL.values()])
            for k, v in zip(SEL, res):
                row[k] = int(v, 16) if k == "decimals" and v and v != "0x" else dec_string(v)
        except RpcError as e:
            row = {"symbol": None, "name": None, "decimals": None}
        except Exception as e:
            log(f"error on {tok}: {str(e)[:80]}; sleeping 5s"); time.sleep(5); continue
        if isinstance(row.get("decimals"), str): row["decimals"] = None
        cur.execute("INSERT INTO tokens (token, symbol, name, decimals, kind) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (token) DO NOTHING",
                    (tok, (row.get("symbol") or "")[:64] or None, (row.get("name") or "")[:128] or None, row.get("decimals"), classify(row.get("symbol"), row.get("name"))))
        if i % 100 == 0: log(f"{i}/{len(todo)}")
    cur.execute(RECLASSIFY); log(f"reclassified {cur.rowcount} tokens")
    cur.execute(DEDUPE); log(f"demoted {cur.rowcount} copycats")
    cur.execute("SELECT kind, count(*) FROM tokens GROUP BY 1 ORDER BY 2 DESC"); log("kinds:", dict(cur.fetchall()))
    cur.execute("SELECT symbol, name FROM tokens WHERE kind='stock' ORDER BY symbol LIMIT 40"); log("stocks:", cur.fetchall())
