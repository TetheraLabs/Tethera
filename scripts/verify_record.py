"""Recompute the hash chain of the desk record from a served feed, so anyone with a key can check that no row was
altered or removed. Each row's hash covers the minute of its timestamp, the strategy, symbol, action, size, reason and
the price to one decimal, chained to the previous row's hash. The per-key fingerprint (seconds, last cent) is outside
the hash by construction, so a fingerprinted feed verifies.

  python scripts/verify_record.py --key tk_...            # fetch from the API and verify
  python scripts/verify_record.py --file record.json      # verify a saved feed (the JSON the API returned)
  python scripts/verify_record.py --file a.json --prev <hash of the row before the first one>"""
import os, sys, json, argparse
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from tethera.keys import record_hash

def verify(rows, prev=""):
    bad = []
    for r in rows:
        h = record_hash(r, prev)
        if h != r.get("hash"): bad.append((r.get("id"), h, r.get("hash")))
        prev = r.get("hash", "")
    return bad, prev

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--key"); ap.add_argument("--file"); ap.add_argument("--prev", default="")
    ap.add_argument("--api", default=os.environ.get("TETHERA_API", "https://api.tetheralabs.com")); a = ap.parse_args()
    if a.file: rows = json.load(open(a.file)); rows = rows.get("rows", rows)
    else:
        import httpx
        rows = []; since = 0
        while True:
            j = httpx.get(f"{a.api}/v1/desk/record", params={"since_id": since, "limit": 1000}, headers={"X-Tethera-Key": a.key or ""}, timeout=60).json()
            if "rows" not in j: raise SystemExit(j)
            rows += j["rows"]
            if j["next_since_id"] == since or not j["rows"]: break
            since = j["next_since_id"]
    bad, last = verify(rows, a.prev)
    print(f"{len(rows)} rows; chain {'intact' if not bad else 'BROKEN'}; last hash {last[:16] if last else '-'}")
    for rid, want, got in bad[:10]: print(f"  row {rid}: computed {want[:16]} served {got[:16] if got else None}")
    sys.exit(1 if bad else 0)

if __name__ == "__main__":
    main()
