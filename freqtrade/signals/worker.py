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
                    # Determine if this was a noise message or a failed signal
                    signal_keywords = ["SHORT", "LONG", "МОНЕТА", "ВХОД", "СТОП", "ЦЕЛЬ"]
                    is_potential_signal = any(kw in text.upper() for kw in signal_keywords)
                    
                    if is_potential_signal:
                        logger.warning(f"Failed to parse potential signal {key}: {text[:50]}...")
                        self.store.mark_status(key, "failed", "Parse failed or unknown format")
                        if self.bot and hasattr(self.bot, 'rpc') and self.bot.rpc:
                            self.bot.rpc.send_msg({
                                'type': RPCMessageType.WARNING,
                                'status': f"⚠️ Signal parsing error (potential signal missed):\n{text[:100]}..."
                            })
                    else:
                        # Chat message or news, ignore quietly
                        logger.info(f"Ignoring non-signal message {key}")
                        self.store.mark_status(key, "skipped", "Non-signal message")
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
                    
                    # Update symbol in DB if it was missing
                    if not row.get('symbol'):
                        try:
                            with self.store._connect() as con:
                                # Remove :USDT suffix for DB storage to match existing convention if needed
                                clean_sym = event.symbol.split('/')[0] if '/' in event.symbol else event.symbol
                                con.execute("UPDATE ingest_queue SET symbol = ? WHERE idempotency_key = ?", (clean_sym, key))
                                con.commit()
                        except Exception as e_db:
                            logger.warning(f"Failed to update symbol in DB for {key}: {e_db}")

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

                            strategy_mode = settings.get('strategy_mode', 'signal')
                            
                            if strategy_mode == 'hybrid':
                                logger.info(f"Hybrid mode active. Marking signal {key} for {event.symbol} as waiting_ta.")
                                self.store.mark_status(key, "waiting_ta")
                                if self.bot and hasattr(self.bot, 'rpc') and self.bot.rpc:
                                    self.bot.rpc.send_msg({
                                        'type': RPCMessageType.STATUS,
                                        'status': f"⏳ Signal {event.symbol} is waiting for TA confirmation (Hybrid mode)"
                                    })
                                continue

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
                                
                                # --- Liquidity and volume check before opening trade ---
                                try:
                                    # Check if we can place orders (basic liquidity check)
                                    ticker = self.bot.exchange.fetch_ticker(event.symbol)
                                    bid_price = ticker['bid']
                                    ask_price = ticker['ask']
                                    
                                    # Basic sanity check - if bid/ask are too far apart, skip trade
                                    if bid_price and ask_price and abs(ask_price - bid_price) / bid_price > 0.05:  # More than 5% spread
                                        logger.warning(f"High spread detected for {event.symbol}: {abs(ask_price - bid_price) / bid_price * 100:.2f}%")
                                    
                                    # Check minimum order size
                                    market = self.bot.exchange.markets[event.symbol]
                                    min_amount = market['limits']['amount']['min']
                                    
                                    if min_amount is not None and trade.amount < min_amount:
                                        logger.warning(f"Order amount too small for {event.symbol}: {trade.amount} < {min_amount}")
                                        self.store.mark_status(key, "failed", f"Order amount too small: {trade.amount} < {min_amount}")
                                        return len(claimed)
                                        
                                except Exception as e:
                                    logger.warning(f"Liquidity check failed for {event.symbol}: {e}")
                                    # Continue with trade anyway, but warn user
                                    pass
                                
                                # Define default percentages for safety (fallback)
                                default_sl_pct = 0.025  # 2.5%
                                default_tp_pct = 0.035  # 3.5%
                                
                                # --- SL Handling ---
                                if event.stop:
                                    sl_price = float(event.stop)
                                    
                                    # --- Liquidation Safety Check ---
                                    # Calculate theoretical liquidation if not yet available from exchange
                                    liq_price = trade.liquidation_price
                                    if not liq_price and trade.open_rate and trade.leverage:
                                        # Very conservative theoretical liquidation calculation
                                        # Short: Entry * (1 + 1/Leverage) | Long: Entry * (1 - 1/Leverage)
                                        # We use a 10% safety margin on the maintenance margin (approx 0.9 factor)
                                        if trade.is_short:
                                            liq_price = trade.open_rate * (1 + 0.95 / trade.leverage)
                                        else:
                                            liq_price = trade.open_rate * (1 - 0.95 / trade.leverage)
                                    
                                    if liq_price:
                                        if trade.is_short and sl_price >= liq_price:
                                            logger.warning(f"Signal SL {sl_price} is beyond liquidation {liq_price:.5f}. Capping SL.")
                                            sl_price = liq_price * 0.99 # 1% buffer
                                        elif not trade.is_short and sl_price <= liq_price:
                                            logger.warning(f"Signal SL {sl_price} is beyond liquidation {liq_price:.5f}. Capping SL.")
                                            sl_price = liq_price * 1.01 # 1% buffer

                                    trade.stop_loss = sl_price
                                    trade.set_custom_data("signal_sl", str(sl_price))
                                    if trade.open_rate:
                                        # Freqtrade uses 'stoploss' field for the relative percentage from open rate
                                        # It should be a negative value (e.g. -0.05 for 5% loss)
                                        sl_ratio = abs((trade.open_rate - sl_price) / trade.open_rate)
                                        trade.stoploss = -sl_ratio
                                        trade.stop_loss_pct = -sl_ratio
                                    
                                    Trade.commit()
                                    
                                    # Place SL order on exchange immediately
                                    try:
                                        # --- Price Check Before Placing SL ---
                                        # If price is already past SL, we shouldn't try to place it (it will fail or trigger immediately)
                                        ticker = self.bot.exchange.fetch_ticker(trade.pair)
                                        current_price = ticker.get('last') or ticker.get('close')
                                        
                                        is_past_sl = False
                                        if current_price:
                                            if trade.is_short and current_price >= sl_price:
                                                is_past_sl = True
                                            elif not trade.is_short and current_price <= sl_price:
                                                is_past_sl = True
                                        
                                        if is_past_sl:
                                            logger.warning(f"Price {current_price} already past SL {sl_price}. EMERGENCY EXIT.")
                                            self.bot.handle_trade_exit(trade, current_price, "stop_loss_hit_immediate")
                                            return len(claimed) # Stop processing this trade
                                            
                                        self.bot.create_stoploss_order(trade, sl_price)
                                        logger.info(f"Signal SL order placed on exchange: {sl_price}")
                                    except Exception as e:
                                        error_msg = str(e).lower()
                                        # BingX error 110412: "Stop Loss price should be greater than the current price" (for short)
                                        # This often means price is already past or at the SL level.
                                        if "110412" in error_msg or "price should be" in error_msg:
                                            logger.warning(f"SL rejected by exchange (likely price past SL): {e}. EMERGENCY EXIT.")
                                            ticker = self.bot.exchange.fetch_ticker(trade.pair)
                                            exit_price = ticker.get('last') or sl_price
                                            self.bot.handle_trade_exit(trade, exit_price, "stop_loss_rejected_market_exit")
                                            return len(claimed)
                                            
                                        logger.error(f"Failed to place signal SL on exchange: {e}")
                                        # Try automatic SL calculation as fallback
                                        logger.warning("Trying automatic SL calculation...")
                                        auto_sl_price = trade.open_rate * (1 - default_sl_pct) if not trade.is_short else trade.open_rate * (1 + default_sl_pct)
                                        
                                        # Ensure auto SL is also within safety limits
                                        if liq_price:
                                            if trade.is_short:
                                                auto_sl_price = min(auto_sl_price, liq_price * 0.99)
                                            else:
                                                auto_sl_price = max(auto_sl_price, liq_price * 1.01)

                                        try:
                                            self.bot.create_stoploss_order(trade, auto_sl_price)
                                            logger.info(f"Auto SL order placed on exchange: {auto_sl_price}")
                                            trade.stop_loss = auto_sl_price
                                            sl_ratio = abs((trade.open_rate - auto_sl_price) / trade.open_rate)
                                            trade.stoploss = -sl_ratio
                                            trade.stop_loss_pct = -sl_ratio
                                        except Exception as e2:
                                            logger.error(f"Failed to place auto SL on exchange: {e2}")
                                            logger.warning("Both SL attempts failed. Freqtrade will retry SL placement in its main loop.")
                                else:
                                    # No SL in signal, calculate auto SL
                                    auto_sl_price = trade.open_rate * (1 - default_sl_pct) if not trade.is_short else trade.open_rate * (1 + default_sl_pct)
                                    trade.stop_loss = auto_sl_price
                                    sl_ratio = abs((trade.open_rate - auto_sl_price) / trade.open_rate)
                                    trade.stoploss = -sl_ratio
                                    trade.stop_loss_pct = -sl_ratio
                                    
                                    Trade.commit()
                                    try:
                                        # Price check for auto SL
                                        ticker = self.bot.exchange.fetch_ticker(trade.pair)
                                        current_price = ticker.get('last') or ticker.get('close')
                                        if current_price:
                                            if (trade.is_short and current_price >= auto_sl_price) or \
                                               (not trade.is_short and current_price <= auto_sl_price):
                                                logger.warning(f"Price {current_price} already past Auto SL {auto_sl_price}. EMERGENCY EXIT.")
                                                self.bot.handle_trade_exit(trade, current_price, "auto_stop_loss_hit_immediate")
                                                return len(claimed)

                                        self.bot.create_stoploss_order(trade, auto_sl_price)
                                        logger.info(f"Auto SL placed (no signal SL): {auto_sl_price}")
                                    except Exception as e:
                                        logger.error(f"Failed to place auto SL: {e}")

                                # --- TP Handling ---
                                # We use a try-except here to ensure SL errors don't prevent TP placement
                                try:
                                    tp_price = None
                                    if event.target:
                                        tp_price = float(event.target)
                                        trade.set_custom_data("signal_tp", str(tp_price))
                                    else:
                                        # Calculate automatic TP
                                        safe_tp_pct = max(default_tp_pct, 0.005) 
                                        auto_tp_price = trade.open_rate * (1 + safe_tp_pct) if not trade.is_short else trade.open_rate * (1 - safe_tp_pct)
                                        tp_price = auto_tp_price
                                        trade.set_custom_data("signal_tp", str(tp_price))
                                        logger.info(f"Auto TP set (no signal TP): {tp_price}")

                                    if tp_price:
                                        exit_side = trade.exit_side
                                        # Use direct CCXT API to avoid Freqtrade wrapper argument issues
                                        tp_order = self.bot.exchange._api.create_order(
                                            symbol=trade.pair,
                                            type="limit",
                                            side=exit_side,
                                            amount=trade.amount,
                                            price=tp_price,
                                            params={"reduceOnly": True}
                                        )
                                        logger.info(f"TP order placed on exchange: {tp_price} (id={tp_order.get('id', '?')})")
                                except Exception as e:
                                    logger.error(f"Failed to place TP on exchange: {e}")
                                    # Fallback to auto TP if signal TP failed
                                    if event.target:
                                        logger.warning("Signal TP failed. Trying automatic TP fallback...")
                                        auto_tp_price = trade.open_rate * (1 + default_tp_pct) if not trade.is_short else trade.open_rate * (1 - default_tp_pct)
                                        try:
                                            self.bot.exchange._api.create_order(
                                                symbol=trade.pair,
                                                type="limit",
                                                side=exit_side,
                                                amount=trade.amount,
                                                price=auto_tp_price,
                                                params={"reduceOnly": True}
                                            )
                                            logger.info(f"Auto TP fallback placed: {auto_tp_price}")
                                            trade.set_custom_data("signal_tp", str(auto_tp_price))
                                        except Exception as e2:
                                            logger.error(f"Auto TP fallback failed: {e2}")
                                
                                # Final commit
                                Trade.commit()
                                
                                logger.info(f"Created Trade {trade.id} for signal {key}. SL: {trade.stop_loss}, TP: {trade.get_custom_data('signal_tp')}")
                                self.store.mark_status(key, "sent")
                            else:
                                self.store.mark_status(key, "failed", "Force entry failed")
                                
                        elif event.type in (SignalType.TAKE_PROFIT, SignalType.STOP_LOSS):
                            from freqtrade.persistence import Trade
                            # Find open trade for this pair
                            # We check both exact match and without :USDT suffix
                            trade = Trade.get_trades([Trade.is_open.is_(True), Trade.pair == event.symbol]).first()
                            if not trade:
                                # Try simple match (e.g. ARB/USDT instead of ARB/USDT:USDT)
                                alt_pair = event.symbol.split(':')[0] if ':' in event.symbol else event.symbol
                                trade = Trade.get_trades([Trade.is_open.is_(True), Trade.pair == alt_pair]).first()
                            
                            if trade:
                                logger.info(f"Signal {event.type.name} for {event.symbol}. Manual exit triggered for trade {trade.id}.")
                                # Use rpc exit for clean execution
                                self.bot.rpc._rpc._rpc_force_exit(str(trade.id))
                                self.store.mark_status(key, "active")
                                logger.info(f"Trade {trade.id} ({trade.pair}) exit command sent via RPC due to channel signal.")
                            else:
                                logger.info(f"Signal {event.type.name} for {event.symbol}, but no open trade found. Skipping.")
                                self.store.mark_status(key, "skipped", "No open trade for this symbol")
                    else:
                        # In tests or if bot is not passed
                        self.store.mark_status(key, "parsed")
            except Exception as e:
                # Rollback database session on error to prevent "transaction rolled back" loop
                try:
                    from freqtrade.persistence import Trade
                    Trade.session.rollback()
                except Exception:
                    pass

                err_msg = str(e)
                is_network_issue = any(x in err_msg.lower() for x in ["network", "timeout", "connection reset", "109400", "timestamp is invalid"])
                
                if "trader is not running" in err_msg.lower():
                    logger.warning(f"Bot is not in RUNNING state while processing {key}. Returning to pending.")
                    self.store.mark_status(key, "pending")
                elif is_network_issue:
                    logger.warning(f"Network issue while processing {key} ({err_msg}). Returning to pending for retry.")
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
        Also reconciles with exchange positions to detect manual trades or lost records.
        """
        try:
            from freqtrade.persistence import Trade
            
            # Find all signals currently in progress (sent or open_exchange)
            conn = self.store._connect()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT idempotency_key, symbol FROM ingest_queue "
                    "WHERE status IN ('sent', 'open_exchange')"
                )
                active_signals = cursor.fetchall()
            finally:
                conn.close()

            for key, symbol in active_signals:
                # Strategy 1: search by enter_tag
                tag_full = f"telegram_{key}"
                tag_short = f"telegram_{key[:8]}"
                trade = Trade.get_trades([Trade.enter_tag.in_([tag_full, tag_short])]).first()
                
                # Strategy 2: if not found by tag, search by pair + is_open
                if not trade and symbol:
                    pair = f"{symbol}/USDT:USDT"
                    trade = Trade.get_trades([
                        Trade.pair == pair,
                        Trade.is_open.is_(True)
                    ]).first()
                
                if trade and not trade.is_open:
                    new_status = "closed_tp"
                    # If profit is negative or SL mentioned in exit reason
                    if (trade.exit_reason and "stop_loss" in trade.exit_reason.lower()) or \
                       (trade.close_profit and trade.close_profit < 0):
                        new_status = "closed_sl"
                        
                    logger.info(f"Trade for signal {key} closed ({trade.exit_reason}). Status: {new_status}")
                    self.store.mark_status(key, new_status, f"Trade closed: {trade.exit_reason}")

        except Exception as e:
            logger.error(f"Error during trade status synchronization: {e}")

    def _reconcile_exchange_positions(self):
        """
        Reconcile open positions on the exchange with ingest_queue and Trade table.
        
        Handles these cases:
        0. Signals stuck in 'processing' (bot crashed) → reset to 'pending'
        1. Signal is 'failed' but position exists on exchange → update to 'open_exchange'
        2. Position on exchange but no Trade record in Freqtrade → log warning
        3. Signal is 'sent' but no Trade record and no exchange position → mark as 'failed'
        """
        if not self.bot or not self.bot.exchange:
            return

        try:
            from freqtrade.persistence import Trade
            
            # 0. Recover signals stuck in 'processing' (bot crashed during processing)
            #    If a signal has been in 'processing' for more than 5 minutes, reset to 'pending'
            conn = self.store._connect()
            try:
                cursor = conn.cursor()
                cursor.row_factory = None
                cursor.execute(
                    "SELECT idempotency_key, updated_at FROM ingest_queue "
                    "WHERE status = 'processing'"
                )
                stuck_rows = cursor.fetchall()
                now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
                for key, updated_at_str in stuck_rows:
                    try:
                        updated_at = datetime.fromisoformat(updated_at_str)
                        stuck_seconds = (now_utc - updated_at).total_seconds()
                        if stuck_seconds > 300:  # 5 minutes
                            logger.warning(
                                f"RECOVER: Signal {key} stuck in 'processing' for "
                                f"{stuck_seconds:.0f}s. Resetting to 'pending'."
                            )
                            self.store.mark_status(key, "pending", f"Recovered from stuck processing ({stuck_seconds:.0f}s)")
                    except Exception:
                        pass
            finally:
                conn.close()

            # 1. Fetch all open positions from the exchange
            try:
                positions = self.bot.exchange.fetch_positions()
            except Exception as e:
                logger.warning(f"Failed to fetch positions from exchange: {e}")
                return

            # Filter to only non-zero positions (actually open)
            open_positions = [
                p for p in positions
                if p.get('contracts') and float(p['contracts']) != 0
            ]

            # 2. Sync with ingest_queue and reconcile TP orders
            self._reconcile_tp_orders()

            if not open_positions:
                return

            # Build a map: symbol -> position
            exchange_pos_map = {}
            for pos in open_positions:
                sym = pos.get('symbol', '')
                if sym:
                    exchange_pos_map[sym] = pos

            # 2. Check 'failed' signals — maybe the position was actually opened
            conn = self.store._connect()
            try:
                cursor = conn.cursor()
                cursor.row_factory = None
                cursor.execute(
                    "SELECT idempotency_key, symbol, text FROM ingest_queue "
                    "WHERE status = 'failed' AND symbol IS NOT NULL"
                )
                failed_signals = cursor.fetchall()
            finally:
                conn.close()

            for key, symbol, text in failed_signals:
                # symbol in DB is like 'ETC', exchange uses 'ETC/USDT:USDT'
                # Try multiple formats
                pair_formats = [
                    f"{symbol}/USDT:USDT",
                    f"{symbol}/USDT",
                ]
                
                matched_pos = None
                for fmt in pair_formats:
                    if fmt in exchange_pos_map:
                        matched_pos = exchange_pos_map[fmt]
                        break

                if matched_pos:
                    contracts = float(matched_pos.get('contracts', 0))
                    if contracts != 0:
                        logger.warning(
                            f"RECONCILE: Signal {key} for {symbol} was 'failed' but position "
                            f"exists on exchange (contracts={contracts}). Updating status."
                        )
                        self.store.mark_status(
                            key, "open_exchange",
                            f"Position found on exchange: {contracts} contracts"
                        )
                        
                        # Also check if there's a Trade record — if not, log it
                        trade = Trade.get_trades([
                            Trade.is_open.is_(True),
                            Trade.pair == matched_pos['symbol']
                        ]).first()
                        
                        if not trade:
                            logger.warning(
                                f"RECONCILE: No Freqtrade Trade record for open position "
                                f"{matched_pos['symbol']} on exchange. "
                                f"Position was likely opened manually or Trade record was lost."
                            )
                            if self.bot and hasattr(self.bot, 'rpc') and self.bot.rpc:
                                self.bot.rpc.send_msg({
                                    'type': RPCMessageType.WARNING,
                                    'status': (
                                        f"⚠️ RECONCILE: Position {matched_pos['symbol']} "
                                        f"exists on exchange but has no Trade record. "
                                        f"Signal {key} updated to open_exchange."
                                    )
                                })

            # 3. Check 'sent' signals — verify Trade record still exists
            conn = self.store._connect()
            try:
                cursor = conn.cursor()
                cursor.row_factory = None
                cursor.execute(
                    "SELECT idempotency_key, symbol FROM ingest_queue "
                    "WHERE status = 'sent' AND symbol IS NOT NULL"
                )
                sent_signals = cursor.fetchall()
            finally:
                conn.close()

            for key, symbol in sent_signals:
                tag_full = f"telegram_{key}"
                tag_short = f"telegram_{key[:8]}"
                trade = Trade.get_trades([Trade.enter_tag.in_([tag_full, tag_short])]).first()
                
                if not trade:
                    # No Trade record — check if position exists on exchange
                    pair_formats = [
                        f"{symbol}/USDT:USDT",
                        f"{symbol}/USDT",
                    ]
                    
                    found_on_exchange = False
                    for fmt in pair_formats:
                        if fmt in exchange_pos_map:
                            pos = exchange_pos_map[fmt]
                            if float(pos.get('contracts', 0)) != 0:
                                found_on_exchange = True
                                logger.warning(
                                    f"RECONCILE: Signal {key} for {symbol} is 'sent' but no Trade "
                                    f"record. Position exists on exchange. Keeping status."
                                )
                                break
                    
                    if not found_on_exchange:
                        # Neither Trade record nor exchange position — likely failed silently
                        logger.warning(
                            f"RECONCILE: Signal {key} for {symbol} is 'sent' but no Trade "
                            f"record and no position on exchange. Marking as failed."
                        )
                        self.store.mark_status(
                            key, "failed",
                            "No Trade record and no position on exchange after reconciliation"
                        )

        except Exception as e:
            logger.error(f"Error during exchange position reconciliation: {e}")

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

    def _reconcile_tp_orders(self):
        """
        Check if open trades have their corresponding TP orders on the exchange.
        If not, place them. Also detects if position is closed on exchange.
        """
        if not self.bot or not self.bot.exchange:
            return

        from freqtrade.persistence import Trade
        open_trades = Trade.get_trades([Trade.is_open.is_(True)]).all()
        if not open_trades:
            return

        try:
            # Fetch all active positions to verify they still exist
            positions = self.bot.exchange.fetch_positions()
            # Symbols in positions can be DOT-USDT or DOT/USDT:USDT
            pos_map = {}
            for p in positions:
                contracts = float(p.get('contracts', 0) or p.get('size', 0))
                if contracts != 0:
                    pos_map[p['symbol']] = p
            
            for trade in open_trades:
                # 1. Check if position still exists on exchange
                # CCXT symbol DOT/USDT:USDT vs positions symbol possibly DOT-USDT
                # We do a loose check
                pair_key = trade.pair
                if pair_key not in pos_map:
                    # Try simple symbol DOT-USDT
                    alt_key = trade.pair.replace('/', '-').split(':')[0]
                    if alt_key not in pos_map:
                        logger.warning(f"RECONCILE: Position for {trade.pair} missing on exchange. Marking trade as closed in DB.")
                        try:
                            from datetime import UTC, datetime
                            ticker = self.bot.exchange.fetch_ticker(trade.pair)
                            close_price = ticker.get('last') or ticker.get('close') or trade.open_rate
                            
                            # Manually close the trade in DB as it's already gone from exchange
                            trade.is_open = False
                            trade.close_date = datetime.now(UTC)
                            trade.close_rate = close_price
                            trade.exit_reason = "external_exit"
                            trade.close_profit = trade.calculate_profit_ratio(close_price)
                            from freqtrade.persistence import Trade
                            Trade.commit()

                            # Notify via RPC if possible
                            if hasattr(self.bot, 'rpc'):
                                from freqtrade.enums import RPCMessageType
                                self.bot.rpc.send_msg({
                                    'type': RPCMessageType.EXIT,
                                    'trade_id': trade.id,
                                    'pair': trade.pair,
                                    'gain': 'profit' if trade.close_profit > 0 else 'loss',
                                    'limit': close_price,
                                    'amount': trade.amount,
                                    'open_rate': trade.open_rate,
                                    'close_rate': close_price,
                                    'profit_amount': trade.close_profit_abs,
                                    'profit_ratio': trade.close_profit,
                                    'exit_reason': 'external_exit'
                                })

                            self._update_signal_status_for_trade(trade, "closed(ext)")
                        except Exception as e_close:
                            logger.error(f"Error closing ghost trade {trade.pair}: {e_close}")
                        continue

                # 2. Check if TP order exists
                if trade.get_custom_data("tp_reconciled") == "1":
                    continue

                tp_price_str = trade.get_custom_data("signal_tp")
                if not tp_price_str:
                    continue
                
                try:
                    tp_price = float(tp_price_str)
                    open_orders = self.bot.exchange._api.fetch_open_orders(trade.pair)
                    
                    tp_order_exists = False
                    for order in open_orders:
                        # On BingX, TP orders can have types like 'limit', 'TAKE_PROFIT', 'TAKE_PROFIT_LIMIT'
                        # We do a loose check based on side and price
                        if order['side'] == trade.exit_side and \
                           abs(float(order['price'] or 0) - tp_price) / tp_price < 0.0001:
                            tp_order_exists = True
                            break
                    
                    if not tp_order_exists:
                        # CRITICAL: Only place reduceOnly TP if position actually exists
                        if trade.pair in pos_map:
                            try:
                                logger.info(f"RECONCILE: TP order missing for {trade.pair} at {tp_price}. Placing now.")
                                self.bot.exchange._api.create_order(
                                    symbol=trade.pair,
                                    type="limit",
                                    side=trade.exit_side,
                                    amount=trade.amount,
                                    price=tp_price,
                                    params={"reduceOnly": True}
                                )
                                logger.info(f"RECONCILE: TP order placed for {trade.pair} at {tp_price}")
                                trade.set_custom_data("tp_reconciled", "1")
                                from freqtrade.persistence import Trade
                                Trade.commit()
                            except Exception as e_place:
                                if "101290" in str(e_place):
                                    logger.info(f"RECONCILE: TP for {trade.pair} already handled by exchange (ReduceOnly limit).")
                                    trade.set_custom_data("tp_reconciled", "1")
                                    from freqtrade.persistence import Trade
                                    Trade.commit()
                                else:
                                    logger.error(f"RECONCILE: Failed to place TP for {trade.pair}: {e_place}")
                        else:
                            logger.debug(f"RECONCILE: Skipping TP for {trade.pair} - position not yet open on exchange.")
                    else:
                        # Order exists, mark as reconciled to stop checking
                        trade.set_custom_data("tp_reconciled", "1")
                        from freqtrade.persistence import Trade
                        Trade.commit()
                except Exception as e:
                    logger.error(f"Error during TP reconciliation for {trade.pair}: {e}")
        except Exception as ge:
            logger.error(f"Global reconciliation error: {ge}")

    def _update_signal_status_for_trade(self, trade, status_prefix):
        if not trade.enter_tag:
            return
        # Tag format: telegram_telegram:1566432615:13346
        # The store expects the idempotency_key (telegram:1566432615:13346)
        sig_key = trade.enter_tag.replace('telegram_', '', 1)
        self.store.mark_status(sig_key, status_prefix)

    def _sync_signal_statuses(self):
        """
        Periodically sync signal statuses with trade outcomes.
        """
        from freqtrade.persistence import Trade
        import sqlite3
        try:
            conn = self.store._connect()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # Sync anything that is not in a final success/expired state
            cursor.execute("SELECT * FROM ingest_queue WHERE status NOT IN ('closed(TP)', 'closed(SL)', 'closed(ext)', 'expired', 'skipped')")
            open_signals_raw = cursor.fetchall()
            open_signals = [dict(r) for r in open_signals_raw]
            conn.close()

            for sig in open_signals:
                # Trade tag is telegram_ + idempotency_key
                tag = f"telegram_{sig['idempotency_key']}"
                
                # Use direct session query for maximum reliability
                try:
                    trade = Trade.session.query(Trade).filter(Trade.enter_tag == tag).first()
                except Exception as e_db:
                    logger.error(f"SYNC: DB error looking up trade for {tag}: {e_db}")
                    continue
                
                new_status = None
                if trade:
                    if trade.is_open:
                        if sig['status'] != 'active':
                            new_status = "active"
                    else:
                        reason = trade.exit_reason or "exit"
                        new_status = f"closed({reason})"
                else:
                    # No trade found. 
                    import datetime
                    occ = datetime.datetime.fromisoformat(sig['occurred_at'])
                    age_min = (datetime.datetime.now() - occ).total_seconds() / 60
                    
                    if sig['status'] in ('ordered', 'active') and age_min > 20:
                        new_status = "expired"
                    elif sig['status'] == 'failed' and age_min > 240: # 4 hours
                        new_status = "expired"

                if new_status and new_status != sig['status']:
                    self.store.mark_status(sig['idempotency_key'], new_status)
                    logger.info(f"SYNC: Updated signal {sig['idempotency_key']} status to {new_status}")
                    
        except Exception as e:
            logger.error(f"Global status sync error: {e}")

    def _run_loop(self):
        logger.info("SignalWorker started")
        import time
        last_sync = 0
        last_reconcile = 0
        last_diag = 0
        while not self._stop_event.is_set():
            try:
                # Reset any signals stuck in 'processing' (e.g. after crash)
                self.store.reset_stuck_signals()
                
                self.process_once()
                
                now = time.time()
                # Sync signal statuses every 120 seconds
                if now - last_sync > 120:
                    self._sync_signal_statuses()
                    last_sync = now
                
                # Reconcile TP orders every 300 seconds (5 mins)
                if now - last_reconcile > 300:
                    self._reconcile_tp_orders()
                    last_reconcile = now
                
                # Diagnostics every 10 minutes
                if now - last_diag > 600:
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
