import logging
from typing import List, Dict, Any
from pathlib import Path
import sqlite3
from fastapi import APIRouter, Depends
from freqtrade.rpc.api_server.deps import get_config, get_rpc_optional

logger = logging.getLogger(__name__)
router = APIRouter()

def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d

@router.get("/signals", tags=["Signals"])
def get_signals(limit: int = 10, offset: int = 0, config: dict = Depends(get_config)) -> Dict[str, Any]:
    try:
        db_path = Path("/freqtrade/user_data/signals.db")
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            conn.row_factory = dict_factory
            cursor = conn.cursor()
            query_filter = "WHERE symbol IS NOT NULL AND (text LIKE '%LONG%' OR text LIKE '%SHORT%')"
            
            cursor.execute(f"SELECT COUNT(*) as total FROM ingest_queue {query_filter}")
            total = cursor.fetchone()["total"]
            
            cursor.execute(
                f"SELECT * FROM ingest_queue {query_filter} ORDER BY occurred_at DESC LIMIT ? OFFSET ?", 
                (limit, offset)
            )
            rows = cursor.fetchall()
        finally:
            conn.close()
            
        # Safe enrichment using ONLY sqlite3
        enriched_rows = []
        db_url = config.get('db_url', 'sqlite:///tradesv3.sqlite')
        trades_db_path = None
        if db_url.startswith('sqlite:///'):
            trades_db_path = Path(db_url.replace('sqlite:///', ''))
            if not trades_db_path.is_absolute():
                trades_db_path = Path(config.get('user_data_dir', 'user_data')) / trades_db_path

        for row in rows:
            enriched_row = dict(row)
            if trades_db_path and trades_db_path.exists():
                try:
                    tag = f"telegram_{row['idempotency_key']}"
                    t_conn = sqlite3.connect(f"file:{trades_db_path}?mode=ro", uri=True)
                    t_conn.row_factory = sqlite3.Row
                    t_cursor = t_conn.cursor()
                    t_cursor.execute("SELECT id, is_open, open_rate, stop_loss, exit_reason, close_rate, leverage FROM trades WHERE enter_tag = ? LIMIT 1", (tag,))
                    t_row = t_cursor.fetchone()
                    if t_row:
                        t_cursor.execute("SELECT price FROM orders WHERE ft_trade_id = ? AND ft_order_side = 'sell' AND status = 'open' LIMIT 1", (t_row['id'],))
                        o_row = t_cursor.fetchone()
                        enriched_row['trade_data'] = {
                            "id": t_row['id'],
                            "is_open": bool(t_row['is_open']),
                            "real_entry": t_row['open_rate'],
                            "real_tp": o_row['price'] if o_row else None,
                            "real_sl": t_row['stop_loss'],
                            "exit_reason": t_row['exit_reason'],
                            "close_rate": t_row['close_rate'] if not t_row['is_open'] else None,
                            "real_leverage": t_row['leverage']
                        }
                    t_conn.close()
                except Exception:
                    pass
            enriched_rows.append(enriched_row)
            
        return {"signals": enriched_rows, "total_count": total, "limit": limit, "offset": offset}
    except Exception as e:
        logger.exception("Error in get_signals")
        return {"error": str(e), "signals": [], "total_count": 0}

@router.get("/signals_settings", tags=["Signals"])
def get_signals_settings(config: dict = Depends(get_config)) -> Dict[str, Any]:
    from freqtrade.signals.queue_store import SignalQueueStore
    return SignalQueueStore(Path("/freqtrade/user_data/signals.db")).get_settings()

@router.post("/signals_settings", tags=["Signals"])
async def update_signals_settings(request: dict, config: dict = Depends(get_config)) -> Dict[str, Any]:
    from freqtrade.signals.queue_store import SignalQueueStore
    store = SignalQueueStore(Path("/freqtrade/user_data/signals.db"))
    for k, v in request.items(): store.save_setting(k, v)
    return {"status": "ok", "settings": store.get_settings()}

@router.get("/klines", tags=["Signals"])
def get_klines(symbol: str = "BTC/USDT:USDT", timeframe: str = "15m", limit: int = 150) -> Dict[str, Any]:
    rpc = get_rpc_optional()
    if not rpc: return {"code": -1, "msg": "RPC not available", "data": []}
    try:
        from datetime import datetime, UTC, timedelta
        from freqtrade.enums import CandleType
        exchange = rpc._freqtrade.exchange
        from freqtrade.exchange import timeframe_to_seconds
        tf_ms = timeframe_to_seconds(timeframe) * 1000
        since_ms = int((datetime.now(UTC) - timedelta(milliseconds=max(tf_ms * limit * 2, 86400000))).timestamp() * 1000)
        df = exchange.get_historic_ohlcv(pair=symbol, timeframe=timeframe, since_ms=since_ms, candle_type=CandleType.FUTURES)
        return {"code": 0, "data": [{"time": int(row.date.timestamp()), "open": row.open, "high": row.high, "low": row.low, "close": row.close} for row in df.tail(limit).itertuples()]}
    except Exception as e:
        return {"code": -1, "msg": str(e), "data": []}
