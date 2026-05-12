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
        for row in rows:
            enriched_row = dict(row)
            tag = f"telegram_{row['idempotency_key']}"
            try:
                # Find trade by tag
                trade = Trade.session.query(Trade).filter(Trade.enter_tag == tag).first()
                if trade:
                    # Find open TP order for this trade
                    tp_price = None
                    for order in trade.orders:
                        if order.ft_order_side == 'sell' and order.status == 'open':
                            tp_price = order.price
                            break
                    
                    enriched_row['trade_data'] = {
                        "id": trade.id,
                        "is_open": trade.is_open,
                        "real_entry": trade.open_rate,
                        "real_tp": tp_price or getattr(trade, 'signal_tp', None), # fallback
                        "real_sl": trade.stop_loss,
                        "exit_reason": trade.exit_reason,
                        "close_rate": trade.close_rate if not trade.is_open else None
                    }
            except Exception as e_enrich:
                logger.warning(f"Enrichment failed for {tag}: {e_enrich}")
            
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
