"""Local HTTP API over the crowding signal. Run: uvicorn tethera.api:app --host 127.0.0.1 --port 8790"""
from fastapi import FastAPI, HTTPException, Query

from . import db, signal

app = FastAPI(title="Tethera", version="0.1")


@app.get("/health")
def health():
    with db.connect() as conn:
        row = conn.execute("SELECT max(block_time), count(*) FROM swaps WHERE block_time > now() - interval '5 minutes'").fetchone()
    return {"latest_swap": row[0].isoformat() if row[0] else None, "swaps_last_5_min": row[1]}


@app.get("/crowding")
def crowding(pool: str = Query(..., min_length=42, max_length=66), direction: str = Query(..., pattern="^(sell0|buy0)$")):
    try:
        return signal.to_dict(signal.crowding(pool, direction))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e)[:200])


@app.get("/herds")
def herds(min_wallets: int = Query(15, ge=5, le=500)):
    return {"herds": signal.current_herds(min_wallets)}
