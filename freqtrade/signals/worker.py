import logging
import time
import threading
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING
from freqtrade.signals.queue_store import SignalQueueStore
from freqtrade.signals.parser import parse_signal_text, SignalType, SignalSide
from freqtrade.enums import RPCMessageType, State

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
        """
        # Сначала применяем настройки аккаунта (Real/Demo/Simulation)
        settings = self.store.get_settings()
        target_mode = settings.get('exchange_mode', 'vst')
        
        # Определяем флаги на основе выбранного режима
        is_dry_run = (target_mode == 'dry_run')
        is_sandbox = (target_mode != 'live') # Для dry_run и vst используем sandbox
        
        if self.bot and self.bot.exchange:
            # Используем локальную переменную или getattr для проверки текущего режима
            current_api_sandbox = getattr(self.bot.exchange._api, 'sandbox', None)
            current_dry_run = self.bot.config.get('dry_run')
            
            # Если любой из флагов не совпадает с желаемым
            if current_api_sandbox != is_sandbox or current_dry_run != is_dry_run:
                # Принудительно меняем URL-адреса, так как set_sandbox_mode иногда тупит
                base_url = 'https://open-api-vst.bingx.com/openApi' if is_sandbox else 'https://open-api.bingx.com/openApi'
                self.bot.exchange._api.urls['api']['public'] = base_url
                self.bot.exchange._api.urls['api']['private'] = base_url
                self.bot.exchange._api_async.urls['api']['public'] = base_url
                self.bot.exchange._api_async.urls['api']['private'] = base_url

                # Настраиваем биржу (стандартным методом тоже на всякий случай)
                self.bot.exchange._api.set_sandbox_mode(is_sandbox)
                self.bot.exchange._api_async.set_sandbox_mode(is_sandbox)
                self.bot.exchange._api.sandbox = is_sandbox
                self.bot.exchange._api_async.sandbox = is_sandbox
                
                # Настраиваем режим Dry Run самого бота
                self.bot.config['dry_run'] = is_dry_run
                self.bot.exchange._config['dry_run'] = is_dry_run
                if hasattr(self.bot.exchange, '_dry_run'):
                    self.bot.exchange._dry_run = is_dry_run
                
                # Сбрасываем кэш рынков и кошельков
                self.bot.exchange._markets = {}
                self.bot.exchange._reload_markets = True
                if hasattr(self.bot, 'wallets'):
                    self.bot.wallets.update()
                    # Пересчитываем стартовый капитал под новый режим
                    self.bot.wallets.start_cap = self.bot.wallets.get_total_stake_amount()
                
                # Понятные логи
                if is_dry_run:
                    mode_name = "ИМИТАЦИЯ (DRY RUN - только внутренние расчеты)"
                elif is_sandbox:
                    mode_name = "ВИРТУАЛЬНАЯ ТОРГОВЛЯ (VST - реальные ордера на демо-счет)"
                else:
                    mode_name = "РЕАЛЬНАЯ ТОРГОВЛЯ (USDT - настоящие деньги)"
                
                logger.info(f"ВНИМАНИЕ! Режим изменен на: {mode_name}")

        if self.bot and self.bot.state != State.RUNNING:
            # Если бот не в RUNNING, не забираем новые сигналы
            return 0
            
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
                    # Проверка TTL (4 часа)
                    occ_dt = datetime.fromisoformat(row['occurred_at'])
                    # occurred_at в базе хранится как naive UTC
                    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
                    age_seconds = (now_utc - occ_dt).total_seconds()
                    
                    if age_seconds > 4 * 3600:
                        logger.warning(f"Сигнал {key} слишком старый ({age_seconds/3600:.1f}ч). Пропускаем.")
                        self.store.mark_status(key, "skipped", f"TTL expired: age {age_seconds/3600:.1f}h")
                        continue

                    logger.info(f"Сигнал {key} успешно распарсен: {event}")
                    if self.bot and hasattr(self.bot, 'rpc') and self.bot.rpc:
                        self.bot.rpc.send_msg({
                            'type': RPCMessageType.STATUS,
                            'status': f"✅ Распарсен сигнал {event.type.name} {event.symbol}"
                        })
                    
                    if self.bot and hasattr(self.bot, 'rpc') and self.bot.rpc:
                        # Исполнение через RPC
                        if event.type == SignalType.ENTRY:
                            from freqtrade.persistence import Trade
                            settings = self.store.get_settings()
                            entry_mode = settings.get('entry_mode', 'single')

                            # Проверка: если уже есть открытая сделка по этой паре
                            existing = Trade.get_trades([Trade.is_open.is_(True), Trade.pair == event.symbol]).first()
                            if existing and entry_mode == 'single':
                                logger.info(f"Сделка по {event.symbol} уже открыта. Пропускаем сигнал {key} (режим Single).")
                                self.store.mark_status(key, "skipped", "Already in trade (Single mode)")
                                continue
                            
                            from freqtrade.enums import SignalDirection
                            
                            # Входим по рынку одним ордером (Market)
                            price = None  # Игнорируем цену сигнала для первого рыночного входа
                            order_side = SignalDirection.SHORT if event.side == SignalSide.SHORT else SignalDirection.LONG
                            
                            settings = self.store.get_settings()
                            stake_amount = None
                            if settings.get('stake_mode') == 'fixed':
                                stake_amount = float(settings.get('stake_fixed_amount', 10.0))
                            elif settings.get('stake_mode') == 'percentage':
                                perc = float(settings.get('stake_percentage', 3.0)) / 100.0
                                stake_currency = self.bot.config.get('stake_currency', 'USDT')
                                try:
                                    free_bal = self.bot.wallets.get_free(stake_currency)
                                    stake_amount = free_bal * perc
                                    logger.info(f"Рассчитан размер входа: {stake_amount} {stake_currency} ({perc*100}% от свободного {free_bal})")
                                except Exception as e:
                                    logger.error(f"Не удалось получить баланс: {e}")
                                    stake_amount = 10.0 # fallback
                            
                            leverage = event.leverage
                            if not leverage:
                                leverage = float(settings.get('default_leverage', 50.0))

                            # Принудительно выставляем ISOLATED (D.4)
                            try:
                                self.bot.exchange.set_margin_mode('ISOLATED', event.symbol)
                            except Exception:
                                pass

                            trade = self.bot.rpc._rpc._rpc_force_entry(
                                pair=event.symbol,
                                price=price,
                                order_type="market",
                                order_side=order_side,
                                stake_amount=stake_amount,
                                enter_tag=f"telegram_{key}",
                                leverage=leverage
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
                                logger.info(f"Сделка для выхода {event.symbol} не найдена. Пропускаем.")
                                self.store.mark_status(key, "skipped", "Open trade not found for exit")
                    else:
                        # В тестах или если bot не передан
                        self.store.mark_status(key, "parsed")
            except Exception as e:
                err_msg = str(e)
                if "trader is not running" in err_msg.lower():
                    logger.warning(f"Бот не в RUNNING при обработке {key}. Возвращаем в pending.")
                    self.store.mark_status(key, "pending")
                else:
                    logger.exception(f"Исключение при парсинге/выполнении сигнала {key}")
                    self.store.mark_status(key, "failed", err_msg)
                    if getattr(self, 'bot', None) and hasattr(self.bot, 'rpc') and self.bot.rpc:
                        self.bot.rpc.send_msg({
                            'type': RPCMessageType.STATUS,
                            'status': f"❌ Ошибка исполнения сигнала {key}:\n`{err_msg}`"
                        })
                
        return len(claimed)

    def _sync_trade_statuses(self):
        """
        Периодически проверяет статусы сделок во Freqtrade и обновляет ingest_queue.
        """
        try:
            from freqtrade.persistence import Trade
            
            # Находим все сигналы, которые сейчас в процессе (sent)
            conn = self.store._connect()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT idempotency_key FROM ingest_queue WHERE status = 'sent'")
                active_signals = [row[0] for row in cursor.fetchall()]
            finally:
                conn.close()

            if not active_signals:
                return

            for key in active_signals:
                tag_full = f"telegram_{key}"
                tag_short = f"telegram_{key[:8]}"
                # Ищем сделку во Freqtrade по любому из тегов
                trade = Trade.get_trades([Trade.enter_tag.in_([tag_full, tag_short])]).first()
                
                if trade:
                    if not trade.is_open:
                        new_status = "closed_tp"
                        # Если профит отрицательный или есть SL в причине выхода
                        if (trade.exit_reason and "stop_loss" in trade.exit_reason.lower()) or \
                           (trade.close_profit and trade.close_profit < 0):
                            new_status = "closed_sl"
                            
                        logger.info(f"Сделка по сигналу {key} закрыта ({trade.exit_reason}). Статус: {new_status}")
                        self.store.mark_status(key, new_status, f"Trade closed: {trade.exit_reason}")
                    else:
                        pass

        except Exception as e:
            logger.error(f"Ошибка при синхронизации статусов сделок: {e}")

    def _run_diagnostic(self):
        """
        Супер-подробная диагностика сети. Пишет в лог тайминги.
        """
        import time
        import socket
        try:
            results = ["--- NETWORK DIAGNOSTIC ---"]
            
            # 1. Проверка прокси-порта
            start = time.time()
            try:
                s = socket.create_connection(("amneziawg2", 1080), timeout=5)
                s.close()
                results.append(f"Proxy connection (amneziawg2:1080): OK ({int((time.time()-start)*1000)}ms)")
            except Exception as e:
                results.append(f"Proxy connection FAILED: {e}")

            if getattr(self, 'bot', None) and self.bot.exchange:
                # 2. Публичный API (без подписи)
                start = time.time()
                try:
                    # Используем _api (внутренний CCXT объект во Freqtrade)
                    self.bot.exchange._api.fetch_time()
                    results.append(f"BingX Public API (fetch_time): OK ({int((time.time()-start)*1000)}ms)")
                except Exception as e:
                    results.append(f"BingX Public API FAILED: {e}")

                # 3. Приватный API (с ключами)
                start = time.time()
                try:
                    self.bot.exchange.get_balances()
                    results.append(f"BingX Private API (get_balances): OK ({int((time.time()-start)*1000)}ms)")
                except Exception as e:
                    results.append(f"BingX Private API FAILED: {e}")
            
            logger.info("\n".join(results))
        except Exception as e:
            logger.error(f"Diagnostic error: {e}")

    def _run_loop(self):
        logger.info("SignalWorker запущен")
        import time
        last_sync = 0
        last_diag = 0
        while not self._stop_event.is_set():
            try:
                self.process_once()
                
                now = time.time()
                # Синхронизация статусов раз в 30 секунд
                if now - last_sync > 30:
                    self._sync_trade_statuses()
                    last_sync = now
                
                # Диагностика раз в 2 минуты (или чаще если надо)
                if now - last_diag > 120:
                    self._run_diagnostic()
                    last_diag = now

            except Exception:
                logger.exception("SignalWorker столкнулся с ошибкой в основном цикле")
            
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
