import logging
from typing import List, Dict, Any
from pathlib import Path
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from freqtrade.rpc.api_server.deps import get_config, get_rpc_optional
from freqtrade.rpc import RPC
from freqtrade.signals.queue_store import SignalQueueStore

logger = logging.getLogger(__name__)

router = APIRouter()

@router.get("/signals", tags=["Signals"])
def get_signals(limit: int = 100, config: dict = Depends(get_config)) -> Dict[str, Any]:
    """
    Возвращает последние сигналы из базы данных.
    """
    try:
        db_path = config["user_data_dir"] / "signals_queue.sqlite"
        store = SignalQueueStore(db_path)
        
        # Получаем последние 100 сигналов (самые свежие первыми)
        conn = store._connect()
        try:
            conn.row_factory = dict_factory
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM ingest_queue ORDER BY occurred_at DESC LIMIT ?", 
                (limit,)
            )
            rows = cursor.fetchall()
        finally:
            conn.close()
            
        return {
            "signals": rows,
            "total_count": len(rows)
        }
    except Exception as e:
        logger.exception("Error fetching signals")
        return {
            "error": str(e),
            "signals": []
        }

def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d

@router.get("/signals_settings", tags=["Signals"])
def get_signals_settings(config: dict = Depends(get_config)) -> Dict[str, Any]:
    try:
        db_path = config["user_data_dir"] / "signals_queue.sqlite"
        store = SignalQueueStore(db_path)
        return store.get_settings()
    except Exception as e:
        logger.exception("Error fetching settings")
        return {"error": str(e)}

@router.post("/signals_settings", tags=["Signals"])
async def update_signals_settings(request: dict, config: dict = Depends(get_config)) -> Dict[str, Any]:
    try:
        db_path = config["user_data_dir"] / "signals_queue.sqlite"
        store = SignalQueueStore(db_path)
        
        # Filter only allowed keys
        allowed_keys = ['stake_mode', 'stake_fixed_amount', 'stake_percentage', 'default_leverage']
        filtered = {k: v for k, v in request.items() if k in allowed_keys}
        
        if filtered:
            store.update_settings(filtered)
            
        return {"status": "ok", "settings": store.get_settings()}
    except Exception as e:
        logger.exception("Error updating settings")
        return {"error": str(e)}


@router.get("/klines", tags=["Signals"])
def get_klines(symbol: str = "BTC/USDT:USDT", timeframe: str = "15m", limit: int = 150) -> Dict[str, Any]:
    """
    Fetch OHLCV candles via Freqtrade's CCXT exchange connection (works regardless of bot state).
    symbol should be in CCXT format: LINK/USDT:USDT
    """
    rpc: RPC | None = get_rpc_optional()
    if rpc is None:
        return {"code": -1, "msg": "RPC not available", "data": []}
    try:
        from datetime import datetime, UTC, timedelta
        from freqtrade.enums import CandleType
        
        exchange = rpc._freqtrade.exchange
        # Using 2 days of history to be sure we have enough for the limit
        since_ms = int((datetime.now(UTC) - timedelta(days=2)).timestamp() * 1000)
        
        df = exchange.get_historic_ohlcv(
            pair=symbol,
            timeframe=timeframe,
            since_ms=since_ms,
            candle_type=CandleType.FUTURES
        )
        
        data = [
            {
                "time": int(row.date.timestamp()),
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close
            }
            for row in df.tail(limit).itertuples()
        ]
        return {"code": 0, "data": data}
    except Exception as e:
        logger.exception("Error fetching klines via exchange")
        return {"code": -1, "msg": str(e), "data": []}
