import logging
import time
import threading
from typing import Optional, TYPE_CHECKING
from freqtrade.signals.queue_store import SignalQueueStore
from freqtrade.signals.parser import parse_signal_text, SignalType, SignalSide
from freqtrade.enums import RPCMessageType

if TYPE_CHECKING:
    from freqtrade.freqtradebot import FreqtradeBot
    from freqtrade.enums import SignalDirection

logger = logging.getLogger(__name__)

class SignalWorker:
    """
    Фоновый воркер для обработки входящих сигналов из БД.
    На этапе MVP (C.3.2) только парсит сообщения и обновляет статус.
    """
    def __init__(self, store: SignalQueueStore, bot: Optional['FreqtradeBot'] = None, sleep_interval: float = 5.0):
        self.store = store
        self.bot = bot
        self.sleep_interval = sleep_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def process_once(self) -> int:
        """
        Забирает 'pending' записи, парсит их и обновляет статусы.
        Возвращает количество обработанных записей.
        """
        claimed = self.store.claim_pending(limit=10)
        if not claimed:
            return 0
            
        for row in claimed:
            key = row["idempotency_key"]
            text = row["text"]
            
            try:
                event = parse_signal_text(text)
                if event is None:
                    # Парсинг не удался
                    logger.warning(f"Не удалось распарсить сигнал {key}: {text[:50]}...")
                    self.store.mark_status(key, "failed", "Parse failed or unknown format")
                    if self.bot and hasattr(self.bot, 'rpc') and self.bot.rpc:
                        self.bot.rpc.send_msg({
                            'type': RPCMessageType.WARNING,
                            'status': f"⚠️ Ошибка парсинга сигнала:\n{text[:100]}..."
                        })
                else:
                    logger.info(f"Сигнал {key} успешно распарсен: {event}")
                    if self.bot and hasattr(self.bot, 'rpc') and self.bot.rpc:
                        self.bot.rpc.send_msg({
                            'type': RPCMessageType.STATUS,
                            'status': f"✅ Распарсен сигнал {event.type.name} {event.symbol}"
                        })
                    
                    if self.bot and hasattr(self.bot, 'rpc') and self.bot.rpc:
                        # Исполнение через RPC
                        if event.type == SignalType.ENTRY:
                            from freqtrade.enums import SignalDirection
                            
                            # Для входа берем нижнюю границу (или единственную цену)
                            price = event.entry_range[0] if event.entry_range else None
                            order_side = SignalDirection.SHORT if event.side == SignalSide.SHORT else SignalDirection.LONG
                            
                            trade = self.bot.rpc._rpc._rpc_force_entry(
                                pair=event.symbol,
                                price=price,
                                order_type="limit" if price else "market",
                                order_side=order_side,
                                enter_tag=f"telegram_{key[:8]}",
                                leverage=event.leverage
                            )
                            
                            if trade:
                                trade.set_custom_data("signal_id", key)
                                if event.stop:
                                    trade.set_custom_data("signal_sl", event.stop)
                                if event.target:
                                    trade.set_custom_data("signal_tp", event.target)
                                logger.info(f"Создан Trade {trade.id} для сигнала {key}")
                                self.store.mark_status(key, "sent")
                            else:
                                self.store.mark_status(key, "failed", "Force entry failed")
                                
                        elif event.type in (SignalType.TAKE_PROFIT, SignalType.STOP_LOSS):
                            from freqtrade.persistence import Trade
                            # Ищем открытую сделку по монете
                            trade = Trade.get_trades([Trade.is_open.is_(True), Trade.pair == event.symbol]).first()
                            if trade:
                                self.bot.rpc._rpc._rpc_force_exit(str(trade.id), ordertype="market")
                                logger.info(f"Закрыт Trade {trade.id} по сигналу {key}")
                                self.store.mark_status(key, "sent")
                            else:
                                logger.warning(f"Сделка для выхода {event.symbol} не найдена")
                                self.store.mark_status(key, "failed", "Open trade not found for exit")
                    else:
                        # В тестах или если bot не передан
                        self.store.mark_status(key, "parsed")
            except Exception as e:
                logger.exception(f"Исключение при парсинге/выполнении сигнала {key}")
                self.store.mark_status(key, "failed", str(e))
                if getattr(self, 'bot', None) and hasattr(self.bot, 'rpc') and self.bot.rpc:
                    self.bot.rpc.send_msg({
                        'type': RPCMessageType.EXCEPTION,
                        'status': f"🚨 Исключение при обработке сигнала:\n{str(e)}"
                    })
                
        return len(claimed)

    def _run_loop(self):
        logger.info("SignalWorker запущен")
        while not self._stop_event.is_set():
            try:
                self.process_once()
            except Exception:
                logger.exception("SignalWorker столкнулся с ошибкой в основном цикле")
            
            # Ждем порциями, чтобы быстрее реагировать на stop_event
            self._stop_event.wait(self.sleep_interval)
        logger.info("SignalWorker остановлен")

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="SignalWorkerThread", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None
