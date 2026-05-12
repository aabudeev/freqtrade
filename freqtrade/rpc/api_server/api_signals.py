import logging
from typing import List, Dict, Any
from pathlib import Path
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from freqtrade.rpc.api_server.deps import get_config, get_rpc_optional, get_rpc
from freqtrade.rpc import RPC
from freqtrade.signals.queue_store import SignalQueueStore
from freqtrade.persistence import Trade

logger = logging.getLogger(__name__)

router = APIRouter()

@router.get("/signals", tags=["Signals"])
def get_signals(limit: int = 10, offset: int = 0, config: dict = Depends(get_config)) -> Dict[str, Any]:
    """
    Возвращает сигналы из базы данных с поддержкой пагинации.
    """
    try:
        db_path = Path("/freqtrade/user_data/signals.db")
        store = SignalQueueStore(db_path)
        
        conn = store._connect()
        try:
            conn.row_factory = dict_factory
            cursor = conn.cursor()
            
            # Получаем общее количество только для РЕАЛЬНЫХ сигналов на ВХОД
            # Исключаем уведомления о тейках/стопах, фильтруя по ключевым словам LONG/SHORT
            query_filter = "WHERE symbol IS NOT NULL AND (text LIKE '%LONG%' OR text LIKE '%SHORT%')"
            
            cursor.execute(f"SELECT COUNT(*) as total FROM ingest_queue {query_filter}")
            total = cursor.fetchone()["total"]
            
            # Получаем только записи сигналов на вход с пагинацией
            cursor.execute(
                f"SELECT * FROM ingest_queue {query_filter} ORDER BY occurred_at DESC LIMIT ? OFFSET ?", 
                (limit, offset)
            )
            rows = cursor.fetchall()
        finally:
            conn.close()
            
        # Enrich signals with real trade data
        enriched_rows = []
        
        # Get path to trades database from config
        db_url = config.get('db_url', 'sqlite:///tradesv3.sqlite')
        trades_db_path = None
        if db_url.startswith('sqlite:///'):
            trades_db_path = Path(db_url.replace('sqlite:///', ''))
            if not trades_db_path.is_absolute():
                trades_db_path = Path(config.get('user_data_dir', 'user_data')) / trades_db_path

        for row in rows:
            enriched_row = dict(row)
            tag = f"telegram_{row['idempotency_key']}"
            
            if trades_db_path and trades_db_path.exists():
                try:
                    # Query trades database directly via sqlite3 for stability
                    import sqlite3
                    t_conn = sqlite3.connect(f"file:{trades_db_path}?mode=ro", uri=True)
                    try:
                        t_conn.row_factory = sqlite3.Row
                        t_cursor = t_conn.cursor()
                        # Find trade by enter_tag
                        t_cursor.execute("SELECT * FROM trades WHERE enter_tag = ? LIMIT 1", (tag,))
                        t_row = t_cursor.fetchone()
                        
                        if t_row:
                            # Try to find open TP order
                            tp_price = None
                            t_cursor.execute(
                                "SELECT price FROM orders WHERE ft_trade_id = ? AND ft_order_side = 'sell' AND status = 'open' LIMIT 1",
                                (t_row['id'],)
                            )
                            o_row = t_cursor.fetchone()
                            if o_row:
                                tp_price = o_row['price']
                            
                            enriched_row['trade_data'] = {
                                "id": t_row['id'],
                                "is_open": bool(t_row['is_open']),
                                "real_entry": t_row['open_rate'],
                                "real_tp": tp_price or t_row.get('signal_tp'), 
                                "real_sl": t_row['stop_loss'],
                                "exit_reason": t_row['exit_reason'],
                                "close_rate": t_row['close_rate'] if not t_row['is_open'] else None
                            }
                    finally:
                        t_conn.close()
                except Exception as e_enrich:
                    logger.debug(f"Enrichment failed for {tag}: {e_enrich}")
            
            enriched_rows.append(enriched_row)
            
        return {
            "signals": enriched_rows,
            "total_count": total,
            "limit": limit,
            "offset": offset
        }
    except Exception as e:
        logger.exception("Error fetching signals")
        return {
            "error": str(e),
            "signals": [],
            "total_count": 0
        }

def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d

@router.get("/signals_settings", tags=["Signals"])
def get_signals_settings(config: dict = Depends(get_config)) -> Dict[str, Any]:
    try:
        db_path = Path("/freqtrade/user_data/signals.db")
        store = SignalQueueStore(db_path)
        return store.get_settings()
    except Exception as e:
        logger.exception("Error fetching settings")
        return {"error": str(e)}

@router.post("/signals_settings", tags=["Signals"])
async def update_signals_settings(request: dict, config: dict = Depends(get_config)) -> Dict[str, Any]:
    try:
        db_path = Path("/freqtrade/user_data/signals.db")
        store = SignalQueueStore(db_path)
        
        logger.info(f"API: Updating settings: {request}")
        
        # Save all provided keys
        for k, v in request.items():
            store.save_setting(k, v)
            
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
        
        from freqtrade.exchange import timeframe_to_seconds
        
        # Estimate how many ms we need based on timeframe and limit
        tf_ms = timeframe_to_seconds(timeframe) * 1000
        # Fetch 2x more than limit to be safe, but at least 7 days for context
        needed_ms = max(tf_ms * limit * 2, 7 * 24 * 60 * 60 * 1000)
        since_ms = int((datetime.now(UTC) - timedelta(milliseconds=needed_ms)).timestamp() * 1000)
        
        try:
            df = exchange.get_historic_ohlcv(
                pair=symbol,
                timeframe=timeframe,
                since_ms=since_ms,
                candle_type=CandleType.FUTURES
            )
        except Exception as e_hist:
            logger.warning(f"Failed to fetch {limit} klines, retrying with limit=200: {e_hist}")
            # Try with a smaller limit and more recent since_ms
            needed_ms_small = max(tf_ms * 200 * 2, 1 * 24 * 60 * 60 * 1000)
            since_ms_small = int((datetime.now(UTC) - timedelta(milliseconds=needed_ms_small)).timestamp() * 1000)
            df = exchange.get_historic_ohlcv(
                pair=symbol,
                timeframe=timeframe,
                since_ms=since_ms_small,
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
