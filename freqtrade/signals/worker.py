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
    Background worker for processing incoming signals from the database.
    Integrates with FreqtradeBot to execute trades based on external signals.
    """
    def __init__(self, store: SignalQueueStore, bot: Optional['FreqtradeBot'] = None, sleep_interval: float = 5.0):
        self.store = store
        self.bot = bot
        self.sleep_interval = sleep_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def process_once(self) -> int:
        """
        Processes pending signals from the queue.
        Handles account mode switching (Live/VST/DryRun) and trade execution.
        """
        # Account settings (Real/Demo/Simulation)
        settings = self.store.get_settings()
        target_mode = settings.get('exchange_mode', 'vst')
        
        # Flags based on mode
        is_dry_run = (target_mode == 'dry_run')
        is_sandbox = (target_mode != 'live') # Use sandbox for dry_run and vst
        
        if self.bot and self.bot.exchange:
            # Check current sandbox status and dry_run flag
            current_api_sandbox = getattr(self.bot.exchange._api, 'sandbox', None)
            current_dry_run = self.bot.config.get('dry_run')
            
            # If any flag doesn't match the desired mode
            if current_api_sandbox != is_sandbox or current_dry_run != is_dry_run:
                # Define base parameters for the mode
                mode_url = 'https://open-api-vst.bingx.com/openApi' if is_sandbox else 'https://open-api.bingx.com/openApi'
                mode_host = 'open-api-vst.bingx.com' if is_sandbox else 'open-api.bingx.com'

                # Force update URLs in exchange object (both sync and async)
                for api_obj in [self.bot.exchange._api, self.bot.exchange._api_async]:
                    # 1. Set hostname
                    api_obj.hostname = mode_host
                    # 2. Set sandbox flag (CCXT standard)
                    api_obj.set_sandbox_mode(is_sandbox)
                    api_obj.sandbox = is_sandbox
                    # 3. Disable verbose logging (avoid flooding)
                    api_obj.verbose = False
                    # 4. Force update urls['api'] dictionary/string
                    if 'api' in api_obj.urls:
                        if isinstance(api_obj.urls['api'], dict):
                            for k in api_obj.urls['api'].keys():
                                api_obj.urls['api'][k] = mode_url
                        else:
                            api_obj.urls['api'] = mode_url
                
                logger.info(f"Active mode: {'SANDBOX/VST' if is_sandbox else 'LIVE/USDT'}")
                
                # Update bot dry_run config
                self.bot.config['dry_run'] = is_dry_run
                self.bot.exchange._config['dry_run'] = is_dry_run
                if hasattr(self.bot.exchange, '_dry_run'):
                    self.bot.exchange._dry_run = is_dry_run
                
                # Reset markets and wallets cache
                self.bot.exchange._markets = {}
                self.bot.exchange._reload_markets = True
                if hasattr(self.bot, 'wallets'):
                    self.bot.wallets.update()
                    # Recalculate start capital for the new mode
                    self.bot.wallets.start_cap = self.bot.wallets.get_total_stake_amount()
                
                # Human-readable logs
                if is_dry_run:
                    mode_name = "SIMULATION (DRY RUN - internal calculations only)"
                elif is_sandbox:
                    mode_name = "VIRTUAL TRADING (VST - real orders on demo account)"
                else:
                    mode_name = "REAL TRADING (USDT - real money)"
                
                logger.info(f"ATTENTION! Mode changed to: {mode_name}")

        if self.bot and self.bot.state != State.RUNNING:
            # If bot is not in RUNNING state, do not process new signals
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
                    # Parsing failed
                    logger.warning(f"Failed to parse signal {key}: {text[:50]}...")
                    self.store.mark_status(key, "failed", "Parse failed or unknown format")
                    if self.bot and hasattr(self.bot, 'rpc') and self.bot.rpc:
                        self.bot.rpc.send_msg({
                            'type': RPCMessageType.WARNING,
                            'status': f"⚠️ Signal parsing error:\n{text[:100]}..."
                        })
                else:
                    # TTL check (4 hours)
                    occ_dt = datetime.fromisoformat(row['occurred_at'])
                    # occurred_at is stored as naive UTC in DB
                    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
                    age_seconds = (now_utc - occ_dt).total_seconds()
                    
                    if age_seconds > 4 * 3600:
                        logger.warning(f"Signal {key} is too old ({age_seconds/3600:.1f}h). Skipping.")
                        self.store.mark_status(key, "skipped", f"TTL expired: age {age_seconds/3600:.1f}h")
                        continue

                    logger.info(f"Signal {key} successfully parsed: {event}")
                    if self.bot and hasattr(self.bot, 'rpc') and self.bot.rpc:
                        self.bot.rpc.send_msg({
                            'type': RPCMessageType.STATUS,
                            'status': f"✅ Parsed signal {event.type.name} {event.symbol}"
                        })
                    
                    if self.bot and hasattr(self.bot, 'rpc') and self.bot.rpc:
                        # Execution via RPC
                        if event.type == SignalType.ENTRY:
                            from freqtrade.persistence import Trade
                            settings = self.store.get_settings()
                            entry_mode = settings.get('entry_mode', 'single')

                            # Check: if there's already an open trade for this pair
                            existing = Trade.get_trades([Trade.is_open.is_(True), Trade.pair == event.symbol]).first()
                            if existing and entry_mode == 'single':
                                logger.info(f"Trade for {event.symbol} is already open. Skipping signal {key} (Single mode).")
                                self.store.mark_status(key, "skipped", "Already in trade (Single mode)")
                                continue
                            
                            from freqtrade.enums import SignalDirection
                            
                            # Market entry with single order
                            price = None  
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
                                    logger.info(f"Calculated stake size: {stake_amount} {stake_currency} ({perc*100}% of free {free_bal})")
                                except Exception as e:
                                    logger.error(f"Failed to get balance: {e}")
                                    stake_amount = 10.0 # fallback
                            
                            leverage = event.leverage
                            if not leverage:
                                leverage = float(settings.get('default_leverage', 50.0))

                            # Force ISOLATED margin mode
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
                                    # Set initial stop-loss immediately for stoploss_on_exchange
                                    trade.stop_loss = float(event.stop)
                                    if trade.open_rate:
                                        trade.stop_loss_pct = (trade.stop_loss / trade.open_rate) - 1
                                    
                                if event.target:
                                    trade.set_custom_data("signal_tp", event.target)
                                
                                logger.info(f"Created Trade {trade.id} for signal {key}. SL: {event.stop}, TP: {event.target}")
                                self.store.mark_status(key, "sent")
                            else:
                                self.store.mark_status(key, "failed", "Force entry failed")
                                
                        elif event.type in (SignalType.TAKE_PROFIT, SignalType.STOP_LOSS):
                            from freqtrade.persistence import Trade
                            # Search for open trade for this coin
                            trade = Trade.get_trades([Trade.is_open.is_(True), Trade.pair == event.symbol]).first()
                            if trade:
                                self.bot.rpc._rpc._rpc_force_exit(str(trade.id), ordertype="market")
                                logger.info(f"Closed Trade {trade.id} via signal {key}")
                                self.store.mark_status(key, "sent")
                            else:
                                logger.info(f"Trade for exit {event.symbol} not found. Skipping.")
                                self.store.mark_status(key, "skipped", "Open trade not found for exit")
                    else:
                        # In tests or if bot is not passed
                        self.store.mark_status(key, "parsed")
            except Exception as e:
                err_msg = str(e)
                if "trader is not running" in err_msg.lower():
                    logger.warning(f"Bot is not in RUNNING state while processing {key}. Returning to pending.")
                    self.store.mark_status(key, "pending")
                else:
                    logger.exception(f"Exception during signal parsing/execution for {key}")
                    self.store.mark_status(key, "failed", err_msg)
                    if getattr(self, 'bot', None) and hasattr(self.bot, 'rpc') and self.bot.rpc:
                        self.bot.rpc.send_msg({
                            'type': RPCMessageType.STATUS,
                            'status': f"❌ Signal execution error {key}:\n`{err_msg}`"
                        })
                
        return len(claimed)

    def _sync_trade_statuses(self):
        """
        Periodically checks trade statuses in Freqtrade and updates ingest_queue.
        """
        try:
            from freqtrade.persistence import Trade
            
            # Find all signals currently in progress (sent)
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
                # Search for trade in Freqtrade by tag
                trade = Trade.get_trades([Trade.enter_tag.in_([tag_full, tag_short])]).first()
                
                if trade:
                    if not trade.is_open:
                        new_status = "closed_tp"
                        # If profit is negative or SL mentioned in exit reason
                        if (trade.exit_reason and "stop_loss" in trade.exit_reason.lower()) or \
                           (trade.close_profit and trade.close_profit < 0):
                            new_status = "closed_sl"
                            
                        logger.info(f"Trade for signal {key} closed ({trade.exit_reason}). Status: {new_status}")
                        self.store.mark_status(key, new_status, f"Trade closed: {trade.exit_reason}")
                    else:
                        pass

        except Exception as e:
            logger.error(f"Error during trade status synchronization: {e}")

    def _run_diagnostic(self):
        """
        Detailed network diagnostics for troubleshooting.
        """
        import time
        import socket
        try:
            results = ["--- NETWORK DIAGNOSTIC ---"]
            
            # 1. Proxy check
            start = time.time()
            try:
                s = socket.create_connection(("amneziawg2", 1080), timeout=5)
                s.close()
                results.append(f"Proxy connection (amneziawg2:1080): OK ({int((time.time()-start)*1000)}ms)")
            except Exception as e:
                results.append(f"Proxy connection FAILED: {e}")

            if getattr(self, 'bot', None) and self.bot.exchange:
                # 2. Public API check
                start = time.time()
                try:
                    self.bot.exchange._api.fetch_time()
                    results.append(f"BingX Public API (fetch_time): OK ({int((time.time()-start)*1000)}ms)")
                except Exception as e:
                    results.append(f"BingX Public API FAILED: {e}")

                # 3. Private API check
                start = time.time()
                try:
                    self.bot.exchange.get_balances()
                    results.append(f"BingX Private API (get_balances): OK ({int((time.time()-start)*1000)}ms)")
                except Exception as e:
                    results.append(f"BingX Private API FAILED: {e}")
            
            logger.info("--- NETWORK DIAGNOSTIC ---")
            for res in results[1:]:
                logger.info(res)
        except Exception as e:
            logger.error(f"Diagnostic error: {e}")

    def _run_loop(self):
        logger.info("SignalWorker started")
        import time
        last_sync = 0
        last_diag = 0
        while not self._stop_event.is_set():
            try:
                self.process_once()
                
                now = time.time()
                # Sync trade statuses every 30 seconds
                if now - last_sync > 30:
                    self._sync_trade_statuses()
                    last_sync = now
                
                # Diagnostics every 2 minutes
                if now - last_diag > 120:
                    self._run_diagnostic()
                    last_diag = now

            except Exception:
                logger.error("SignalWorker encountered an error in main loop")
            
            self._stop_event.wait(self.sleep_interval)
        logger.info("SignalWorker stopped")

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
