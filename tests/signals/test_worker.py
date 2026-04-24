import pytest
from pathlib import Path
from freqtrade.signals.queue_store import SignalQueueStore
from freqtrade.signals.history_export import SignalIngestEvent
from freqtrade.signals.worker import SignalWorker
from datetime import datetime, timezone

def _ev(key: str, text: str) -> SignalIngestEvent:
    return SignalIngestEvent(
        source="telegram",
        text=text,
        occurred_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None),
        idempotency_key=key,
        replay_line_number=0,
        raw_line="{}",
    )

def test_worker_process_once(tmp_path: Path):
    store = SignalQueueStore(tmp_path / "q.sqlite")
    
    # Валидный сигнал
    valid_text = "LONG\nМонета: DOGE\nВход: 0.150\nЦель: 0.180\nСтоп: 0.140"
    store.enqueue(_ev("key1", valid_text))
    
    # Невалидный сигнал (мусор)
    store.enqueue(_ev("key2", "какой-то текст не по формату"))
    
    worker = SignalWorker(store)
    
    # Обрабатываем
    processed = worker.process_once()
    assert processed == 2
    
    # Проверяем статусы
    assert store.count_by_status("parsed") == 1
    assert store.count_by_status("failed") == 1
    assert store.pending_count() == 0
    
    # Повторный вызов (очередь пуста)
    assert worker.process_once() == 0

from unittest.mock import MagicMock, patch

@patch("freqtrade.persistence.Trade.get_trades")
def test_worker_with_bot(mock_get_trades, tmp_path: Path):
    store = SignalQueueStore(tmp_path / "q2.sqlite")
    
    # Вход
    store.enqueue(_ev("key1", "LONG\nМонета: DOGE\nВход: 0.150\nЦель: 0.180\nСтоп: 0.140\nПлечо: 10x"))
    
    # Выход
    store.enqueue(_ev("key2", "DOGE - тейк ✅"))

    mock_bot = MagicMock()
    mock_trade = MagicMock()
    mock_trade.id = 123
    mock_bot.rpc._rpc._rpc_force_entry.return_value = mock_trade
    
    # Мок поиска трейда для закрытия
    mock_query = MagicMock()
    mock_query.first.return_value = mock_trade
    mock_get_trades.return_value = mock_query
    
    worker = SignalWorker(store, bot=mock_bot)
    
    processed = worker.process_once()
    assert processed == 2
    
    # Проверка вызова force_entry
    mock_bot.rpc._rpc._rpc_force_entry.assert_called_once()
    args, kwargs = mock_bot.rpc._rpc._rpc_force_entry.call_args
    assert kwargs["pair"] == "DOGE/USDT:USDT"
    assert kwargs["leverage"] == 10
    
    # Проверка custom data (signal_id, signal_sl, signal_tp)
    assert mock_trade.set_custom_data.call_count == 3
    
    # Проверка вызовов отправки Telegram-сообщений
    # Мы ожидаем 2 успешных парсинга, то есть 2 вызова send_msg со STATUS
    assert mock_bot.rpc.send_msg.call_count == 2
    
    # Проверка вызова force_exit
    mock_bot.rpc._rpc._rpc_force_exit.assert_called_once_with("123", ordertype="market")
    
    # Проверка статуса в очереди
    assert store.count_by_status("sent") == 2
