import logging
from typing import List, Dict, Any
from pathlib import Path
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from freqtrade.rpc.api_server.deps import get_config
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
        with store._connect() as conn:
            conn.row_factory = dict_factory
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM ingest_queue ORDER BY occurred_at DESC LIMIT ?", 
                (limit,)
            )
            rows = cursor.fetchall()
            
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
