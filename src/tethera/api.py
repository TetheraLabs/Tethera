"""Local HTTP API over the crowding signal. Run: uvicorn tethera.api:app --host 127.0.0.1 --port 8790"""
import json, os
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query

from . import db, signal
from .rpc import live_rpc

HEALTH_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "health.jsonl")

app = FastAPI(title="Tethera", version="0.1")


@app.get("/health")
def health():
    with db.connect() as conn:
        row = conn.execute("""SELECT max(block_time), max(block_number), count(*)
                              FROM swaps WHERE block_time > now() - interval '5 minutes'""").fetchone()
    latest, latest_block, n = row
    out = {"latest_swap": latest.isoformat() if latest else None, "swaps_last_5_min": n,
           "lag_seconds": round((datetime.now(timezone.utc) - latest).total_seconds()) if latest else None,
           "lag_blocks": None, "rpc_endpoint": None}
    try:
        rpc = live_rpc(retries=2)
        head = rpc.block_number()
        out["rpc_endpoint"] = rpc.active.split("/")[2]           # hostname only; the paid URL carries a key
        out["lag_blocks"] = head - latest_block if latest_block else None
    except Exception as e:  # noqa: BLE001
        out["rpc_error"] = type(e).__name__
    try:
        with open(HEALTH_LOG, "rb") as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 8192))
            lines = f.read().splitlines()
            out["last_check"] = json.loads(lines[-1]) if lines else None
    except (OSError, ValueError):
        out["last_check"] = None
    return out


@app.get("/crowding")
def crowding(pool: str = Query(..., min_length=42, max_length=66), direction: str = Query(..., pattern="^(sell0|buy0)$")):
    try:
        return signal.to_dict(signal.crowding(pool, direction))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e)[:200])


@app.get("/herds")
def herds(min_wallets: int = Query(15, ge=5, le=500)):
    return {"herds": signal.current_herds(min_wallets)}
