"""Key administration from the server: list keys, revoke one, see burns.

  python scripts/keys_admin.py list [--all]
  python scripts/keys_admin.py revoke <key_hash_prefix> --reason "redistribution seen at ..."
  python scripts/keys_admin.py burns
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from tethera import db, keys

def arg(n, d=None): return sys.argv[sys.argv.index(n) + 1] if n in sys.argv else d

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    with db.connect() as conn:
        if cmd == "list":
            where = "" if "--all" in sys.argv else "WHERE NOT disabled AND (expires_at IS NULL OR expires_at > now())"
            for r in conn.execute(f"SELECT left(key_hash, 12), label, tier, requests_per_minute, wallet, created_at::date, expires_at::date, disabled, revoked_reason FROM api_keys {where} ORDER BY created_at DESC LIMIT 200").fetchall():
                print(" | ".join("" if v is None else str(v) for v in r))
        elif cmd == "revoke":
            n = keys.revoke(sys.argv[2], arg("--reason", "revoked")); print(f"{n} key(s) revoked")
        elif cmd == "burns":
            for r in conn.execute("SELECT tx_hash, log_index, wallet, amount, left(key_hash, 12), created_at FROM key_burns ORDER BY created_at DESC LIMIT 100").fetchall():
                print(" | ".join(str(v) for v in r))

if __name__ == "__main__":
    main()
