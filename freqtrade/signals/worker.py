import logging
import time
import threading
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING
from freqtrade.signals.queue_store import SignalQueueStore
from freqtrade.signals.parser import parse_signal_text, SignalType, SignalSide
from freqtrade.enums import RPCMessageType, State, SignalDirection

if TYPE_CHECKING:
    from freqtrade.freqtradebot import FreqtradeBot

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
        
        # Silence noisy exchange loggers
        logging.getLogger('freqtrade.exchange').setLevel(logging.WARNING)
        logging.getLogger('freqtrade.exchange.common').setLevel(logging.WARNING)
        logging.getLogger('urllib3').setLevel(logging.WARNING)
        
        # Entry range retry tracker: {signal_key: {'count': N, 'last_ts': timestamp}}
        self._range_retries: dict = {}

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
                    self.process_signal(key, event, row, is_emergency=False)
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

    def process_signal(self, key, event, row, is_emergency=False):
        """
        Execute or repair a signal.
        If is_emergency=True, skips entry and only ensures SL/TP exist.
        """
        try:
            if not is_emergency:
                # TTL check (1 hour)
                from datetime import datetime, timezone
                occ_dt = datetime.fromisoformat(row['occurred_at'])
                now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
                age_seconds = (now_utc - occ_dt).total_seconds()
                
                if age_seconds > 1 * 3600:
                    logger.warning(f"Signal {key} is too old ({age_seconds/3600:.1f}h). Skipping.")
                    self.store.mark_status(key, "skipped", f"TTL expired: age {age_seconds/3600:.1f}h")
                    return

            logger.info(f"{'[EMERGENCY] ' if is_emergency else ''}Signal {key} successfully parsed: {event}")
            
            # Update symbol in DB if it was missing
            if not row.get('symbol'):
                try:
                    with self.store._connect() as con:
                        clean_sym = event.symbol.split('/')[0] if '/' in event.symbol else event.symbol
                        con.execute("UPDATE ingest_queue SET symbol = ? WHERE idempotency_key = ?", (clean_sym, key))
                        con.commit()
                except Exception as e_db:
                    logger.warning(f"Failed to update symbol in DB for {key}: {e_db}")

            if self.bot and hasattr(self.bot, 'rpc') and self.bot.rpc:
                if not is_emergency:
                    self.bot.rpc.send_msg({
                        'type': RPCMessageType.STATUS,
                        'status': f"✅ Parsed signal {event.type.name} {event.symbol}"
                    })

                # Execution via RPC
                if event.type == SignalType.ENTRY:
                    from freqtrade.persistence import Trade
                    settings = self.store.get_settings()
                    entry_mode = settings.get('entry_mode', 'single')

                    # Check: if there's already an open trade for this pair
                    trade = Trade.get_trades([Trade.is_open.is_(True), Trade.pair == event.symbol]).first()
                    
                    if not trade and not is_emergency:
                        # --- ENTRY RANGE CHECK (slippage protection) ---
                        # Tolerance: 0.5% buffer to account for BingX vs Binance/Bybit price differences
                        ENTRY_RANGE_TOLERANCE = 0.005  # 0.5%
                        if event.entry_range and len(event.entry_range) == 2:
                            try:
                                ticker = self.bot.exchange.fetch_ticker(event.symbol)
                                current_price = ticker.get('last', 0)
                                range_low, range_high = float(event.entry_range[0]), float(event.entry_range[1])
                                # Expand range by tolerance to account for cross-exchange price diff
                                range_low_adj = range_low * (1 - ENTRY_RANGE_TOLERANCE)
                                range_high_adj = range_high * (1 + ENTRY_RANGE_TOLERANCE)
                                
                                if current_price and (current_price < range_low_adj or current_price > range_high_adj):
                                    # --- Throttled retry: max 10 attempts, 1 per minute ---
                                    retry_info = self._range_retries.get(key, {'count': 0, 'last_ts': 0})
                                    now_ts = datetime.now().timestamp()
                                    
                                    if retry_info['count'] >= 10:
                                        # Exhausted retries — permanent skip
                                        skip_msg = f"Price {current_price} outside entry range [{range_low} - {range_high}] after 10 retries (10 min)"
                                        logger.warning(f"Signal {key}: {skip_msg}. Final skip.")
                                        self.store.mark_status(key, "skipped", skip_msg)
                                        self._range_retries.pop(key, None)
                                        if self.bot and hasattr(self.bot, 'rpc') and self.bot.rpc:
                                            self.bot.rpc.send_msg({
                                                'type': RPCMessageType.STATUS,
                                                'status': f"⚠️ Skipped {event.symbol}: {skip_msg}"
                                            })
                                        return
                                    
                                    if now_ts - retry_info['last_ts'] < 60:
                                        # Too soon since last retry — skip silently
                                        return
                                    
                                    # Record this retry attempt
                                    retry_info['count'] += 1
                                    retry_info['last_ts'] = now_ts
                                    self._range_retries[key] = retry_info
                                    
                                    skip_msg = f"Price {current_price} outside entry range [{range_low} - {range_high}] (attempt {retry_info['count']}/10)"
                                    logger.warning(f"Signal {key}: {skip_msg}. Will retry in 1 min.")
                                    self.store.mark_status(key, "pending", skip_msg)
                                    if retry_info['count'] == 1 and self.bot and hasattr(self.bot, 'rpc') and self.bot.rpc:
                                        self.bot.rpc.send_msg({
                                            'type': RPCMessageType.STATUS,
                                            'status': f"⏳ {event.symbol}: Price outside range — retrying for 10 min"
                                        })
                                    return
                                else:
                                    if current_price < range_low or current_price > range_high:
                                        logger.info(f"Signal {key}: Price {current_price} slightly outside [{range_low} - {range_high}] but within tolerance. Proceeding.")
                                    else:
                                        logger.info(f"Signal {key}: Price {current_price} is within entry range [{range_low} - {range_high}]. Proceeding.")
                            except Exception as e_range:
                                logger.warning(f"Signal {key}: Could not check entry range: {e_range}. Proceeding with entry anyway.")

                        # --- SPREAD CHECK (bid/ask safety) ---
                        try:
                            ticker = self.bot.exchange.fetch_ticker(event.symbol)
                            bid = ticker.get('bid')
                            ask = ticker.get('ask')
                            if bid and ask:
                                spread = (ask - bid) / bid
                                if spread > 0.02:  # 2% limit
                                    msg = f"Spread too high ({spread*100:.2f}%) for {event.symbol}. Signal skipped for safety."
                                    logger.warning(msg)
                                    self.store.mark_status(key, "skipped", msg)
                                    if self.bot and hasattr(self.bot, 'rpc') and self.bot.rpc:
                                        self.bot.rpc.send_msg({
                                            'type': RPCMessageType.STATUS,
                                            'status': f"⚠️ {msg}"
                                        })
                                    return
                        except Exception as e_spread:
                            logger.warning(f"Could not check spread for {event.symbol}: {e_spread}")

                        # --- NORMAL ENTRY ---
                        from freqtrade.enums import SignalDirection
                        price = None  
                        order_side = SignalDirection.SHORT if event.side == SignalSide.SHORT else SignalDirection.LONG
                        
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
                        
                        # Force leverage to be the one specified in the settings (e.g. 18x), 
                        # completely ignoring the leverage parsed from the telegram signal text.
                        leverage = float(settings.get('default_leverage', 10.0))

                        # Force ISOLATED margin mode
                        try:
                            self.bot.exchange.set_margin_mode('ISOLATED', event.symbol)
                        except Exception:
                            pass

                        trade = self.bot.rpc._rpc._rpc_force_entry(
                            pair=event.symbol,
                            price=None,
                            order_type="market",
                            order_side=order_side,
                            stake_amount=stake_amount,
                            enter_tag=f"telegram_{key}",
                            leverage=leverage
                        )
                    
                    if trade:
                        if not trade.get_custom_data("signal_id"):
                            trade.set_custom_data("signal_id", key)
                        
                        # Define default percentages for safety (fallback)
                        default_sl_pct = 0.025  # 2.5%
                        default_tp_pct = 0.035  # 3.5%
                        
                        # --- SL Handling ---
                        has_sl = any(o.ft_order_side == 'stoploss' and o.ft_is_open for o in trade.orders)
                        if not has_sl:
                            sl_price = float(event.stop) if event.stop else None
                            
                            # Calculate theoretical liquidation if not yet available
                            liq_price = trade.liquidation_price
                            if not liq_price and trade.open_rate and trade.leverage:
                                if trade.is_short:
                                    liq_price = trade.open_rate * (1 + 0.95 / trade.leverage)
                                else:
                                    liq_price = trade.open_rate * (1 - 0.95 / trade.leverage)
                            
                            if sl_price and liq_price:
                                if trade.is_short and sl_price >= liq_price:
                                    sl_price = liq_price * 0.99
                                elif not trade.is_short and sl_price <= liq_price:
                                    sl_price = liq_price * 1.01

                            # Auto SL if signal has no stop price
                            if not sl_price:
                                sl_price = trade.open_rate * (1 - default_sl_pct) if not trade.is_short else trade.open_rate * (1 + default_sl_pct)
                                logger.info(f"Auto SL set (no signal SL): {sl_price}")

                            trade.set_custom_data("signal_sl", str(sl_price))
                            leverage = trade.leverage or 1.0
                            sl_price_ratio = abs((trade.open_rate - sl_price) / trade.open_rate)
                            sl_trade_ratio = sl_price_ratio * leverage
                            trade.stop_loss = sl_price
                            trade.stoploss = -sl_trade_ratio
                            trade.stop_loss_pct = -sl_trade_ratio
                            trade.initial_stop_loss = sl_price
                            trade.initial_stop_loss_pct = -sl_trade_ratio
                            Trade.commit()
                            
                            # --- Price Check Before Placing SL (Emergency Exit) ---
                            try:
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
                                    self.bot.rpc._rpc._rpc_force_exit(str(trade.id))
                                    return
                            except Exception as e_check:
                                logger.warning(f"Could not check price vs SL: {e_check}")
                            
                            try:
                                # Place SL order on exchange immediately
                                self.bot.create_stoploss_order(trade, sl_price)
                                logger.info(f"Signal SL order placed: {sl_price}")
                            except Exception as e:
                                logger.error(f"Failed to place signal SL: {e}")
                                # Fallback to auto SL
                                auto_sl_price = trade.open_rate * (1 - default_sl_pct) if not trade.is_short else trade.open_rate * (1 + default_sl_pct)
                                try:
                                    self.bot.create_stoploss_order(trade, auto_sl_price)
                                    logger.info(f"Auto SL fallback placed: {auto_sl_price}")
                                except Exception:
                                    pass

                        # --- TP Handling ---
                        has_tp = any(o.ft_order_side == trade.exit_side and o.ft_is_open for o in trade.orders)
                        if not has_tp:
                            tp_price = float(event.target) if event.target else None

                            # Auto TP if signal has no target price
                            if not tp_price:
                                safe_tp_pct = max(default_tp_pct, 0.005)
                                tp_price = trade.open_rate * (1 + safe_tp_pct) if not trade.is_short else trade.open_rate * (1 - safe_tp_pct)
                                logger.info(f"Auto TP set (no signal TP): {tp_price}")

                            trade.set_custom_data("signal_tp", str(tp_price))
                            symbol = trade.pair.replace("/", "-").split(":")[0]
                            exit_side = trade.exit_side
                            logger.info(f"Placing TP: {symbol} at {tp_price}")
                            try:
                                tp_order = self.bot.exchange._api.swapV2PrivatePostTradeOrder({
                                    "symbol": symbol,
                                    "side": exit_side.upper(),
                                    "positionSide": "BOTH",
                                    "type": "LIMIT",
                                    "quantity": trade.amount,
                                    "price": tp_price,
                                    "reduceOnly": "true"
                                })
                                if tp_order and 'data' in tp_order:
                                    new_id = tp_order['data'].get('orderId')
                                    if not new_id and isinstance(tp_order['data'].get('order'), dict):
                                        new_id = tp_order['data']['order'].get('orderId')
                                    if new_id:
                                        # Register Order in DB
                                        from freqtrade.persistence import Order
                                        tp_order_obj = Order(
                                            ft_trade_id=trade.id,
                                            ft_pair=trade.pair,
                                            ft_is_open=True,
                                            ft_order_side=exit_side,
                                            ft_amount=trade.amount,
                                            ft_price=tp_price,
                                            order_id=str(new_id),
                                            status='open',
                                            symbol=trade.pair,
                                            order_type='limit',
                                            side=exit_side,
                                            amount=trade.amount,
                                            filled=0.0,
                                            remaining=trade.amount,
                                            price=tp_price,
                                            order_date=datetime.now(timezone.utc),
                                        )
                                        trade.orders.append(tp_order_obj)
                                        Trade.commit()
                                        logger.info(f"TP order {new_id} registered.")
                            except Exception as e:
                                logger.error(f"Failed to place TP: {e}")

                        Trade.commit()
                        if not is_emergency:
                            self.store.mark_status(key, "sent")
                    elif not is_emergency:
                        self.store.mark_status(key, "failed", "Force entry failed")

                elif event.type in (SignalType.TAKE_PROFIT, SignalType.STOP_LOSS):
                    from freqtrade.persistence import Trade
                    trade = Trade.get_trades([Trade.is_open.is_(True), Trade.pair == event.symbol]).first()
                    if trade:
                        try:
                            self.bot.rpc._rpc._rpc_force_exit(str(trade.id))
                        except Exception as e:
                            logger.warning(f"Note: _rpc_force_exit raised an exception (often safe to ignore if trade actually closed): {e}")
                        
                        self.store.mark_status(key, "active")
                        logger.info(f"Exit sent for trade {trade.id}.")
                    else:
                        self.store.mark_status(key, "skipped", "No open trade")
            else:
                self.store.mark_status(key, "parsed")
        except Exception as e:
            logger.exception(f"Error in process_signal: {e}")
            if not is_emergency:
                raise e


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
                    exit_reason = trade.exit_reason or "unknown"
                    profit_pct = trade.close_profit_pct or 0.0
                    
                    if ("stop_loss" in exit_reason.lower()) or (trade.close_profit and trade.close_profit < 0):
                        new_status = "closed_sl"
                        
                    logger.info(f"Trade for signal {key} closed ({exit_reason}). Status: {new_status}")
                    self.store.mark_status(key, new_status, f"Trade closed: {exit_reason}")
                    
                    # Manual Telegram notification for exit
                    if self.bot and hasattr(self.bot, 'rpc') and self.bot.rpc:
                        try:
                            side_emoji = "🟢" if profit_pct >= 0 else "🔴"
                            msg = (
                                f"{side_emoji} *Trade Closed: {trade.pair}*\n"
                                f"Reason: `{exit_reason}`\n"
                                f"Profit: `{profit_pct:.2%}`\n"
                                f"Signal Key: `{key}`"
                            )
                            self.bot.rpc.send_msg({
                                'type': RPCMessageType.STATUS,
                                'status': msg
                            })
                        except Exception as e_msg:
                            logger.error(f"Failed to send exit notification: {e_msg}")

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

            # 2. Check open positions

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
        from freqtrade.persistence import Trade
        import sqlite3
        try:
            Trade.session.remove() # Ensure fresh session for each sync
            
            conn = self.store._connect()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Sync anything that is not in a final success/expired state
            cursor.execute(
                "SELECT * FROM ingest_queue "
                "WHERE status NOT LIKE 'closed%' "
                "AND status NOT IN ('expired', 'skipped', 'failed_liquidation')"
            )
            open_signals_raw = cursor.fetchall()
            open_signals = [dict(r) for r in open_signals_raw]
            conn.close()

            for sig in open_signals:
                tag = f"telegram_{sig['idempotency_key']}"
                
                try:
                    # Look up trade by tag
                    trade = Trade.session.query(Trade).filter(Trade.enter_tag == tag).first()
                    if not trade:
                        # Fallback: search by pair if it was a very recent entry (within 5 mins)
                        import datetime
                        occ = datetime.datetime.fromisoformat(sig['occurred_at'])
                        if (datetime.datetime.now() - occ).total_seconds() < 300:
                            trade = Trade.session.query(Trade).filter(
                                Trade.pair == sig['symbol'], 
                                Trade.is_open == True
                            ).order_by(Trade.id.desc()).first()
                except Exception as e_db:
                    logger.error(f"SYNC: DB error looking up trade for {tag}: {e_db}")
                    continue
                
                new_status = None
                if trade:
                    if trade.is_open:
                        if sig['status'] != 'active':
                            new_status = "active"
                    else:
                        # Trade is closed!
                        reason = str(trade.exit_reason) if trade.exit_reason else "exit"
                        new_status = f"closed({reason})"
                        logger.info(f"SYNC: Detected closed trade {trade.id} for signal {sig['idempotency_key']}")
                else:
                    # No trade record found in DB
                    import datetime
                    occ = datetime.datetime.fromisoformat(sig['occurred_at'])
                    age_min = (datetime.datetime.now() - occ).total_seconds() / 60
                    
                    if sig['status'] in ('ordered', 'active') and age_min > 20:
                        new_status = "expired"
                    elif sig['status'] == 'failed' and age_min > 240:
                        new_status = "expired"

                if new_status and new_status != sig['status']:
                    self.store.mark_status(sig['idempotency_key'], new_status)
                    logger.info(f"SYNC: Updated signal {sig['idempotency_key']} status to {new_status}")
                    
        except Exception as e:
            logger.error(f"Global status sync error: {e}")

    def _emergency_reconcile(self):
        """One-time check for open trades without SL/TP on startup"""
        from freqtrade.persistence import Trade
        try:
            open_trades = Trade.session.query(Trade).filter(Trade.is_open == True).all()
            if not open_trades:
                return
            
            logger.info(f"SignalWorker: Emergency check for {len(open_trades)} open trades...")
            for trade in open_trades:
                has_sl = any(o.ft_order_side == 'stoploss' and o.ft_is_open for o in trade.orders)
                has_tp = any(o.ft_order_side == trade.exit_side and o.ft_is_open for o in trade.orders)
                
                if not has_sl or not has_tp:
                    # Get signal key from enter_tag (format: telegram_telegram:123:456)
                    tag = trade.enter_tag or ""
                    if tag.startswith("telegram_"):
                        sig_key = tag.replace("telegram_", "")
                        logger.warning(f"SignalWorker: Trade {trade.pair} (# {trade.id}) is missing SL or TP! Attempting immediate fix using signal {sig_key}...")
                        
                        # Get original signal to recover SL/TP prices by re-parsing the text
                        conn = self.store._connect()
                        cursor = conn.cursor()
                        cursor.execute("SELECT text FROM ingest_queue WHERE idempotency_key = ?", (sig_key,))
                        row = cursor.fetchone()
                        conn.close()
                        
                        if row:
                            signal_text = row[0]
                            # Re-parse the original signal text to get SL/TP
                            event = parse_signal_text(signal_text)
                            if event:
                                logger.info(f"SignalWorker: Successfully recovered signal data for {trade.pair}. SL: {event.stop}, TP: {event.target}")
                                # This will attempt to place missing SL/TP
                                self.process_signal(sig_key, event, {'occurred_at': trade.open_date.isoformat()}, is_emergency=True)
                            else:
                                logger.error(f"SignalWorker: Failed to re-parse original signal text for {sig_key}")
                        else:
                            logger.error(f"SignalWorker: Could not find original signal {sig_key} in database.")
        except Exception as e:
            logger.exception(f"Emergency reconcile error: {e}")

    def _run_loop(self):
        logger.info("SignalWorker started")
        import time
        last_sync = 0
        last_reconcile = 0
        last_diag = 0
        
        # Initial wait for bot to fully initialize
        self._stop_event.wait(5)
        self._emergency_reconcile()

        while not self._stop_event.is_set():
            try:
                now = time.time()
                if hasattr(self, '_backoff_until') and now < self._backoff_until:
                    self._stop_event.wait(5)
                    continue

                # Reset any signals stuck in 'processing' (e.g. after crash)
                self.store.reset_stuck_signals()
                
                self.process_once()
                
                # Sync signal statuses every 120 seconds
                if now - last_sync > 120:
                    self._sync_signal_statuses()
                    last_sync = now
                
                # Reconcile exchange positions every 5 minutes
                if now - last_reconcile > 300:
                    self._reconcile_exchange_positions()
                    last_reconcile = now
                
                # Diagnostics every 10 minutes
                if now - last_diag > 600:
                    self._run_diagnostic()
                    last_diag = now

            except Exception as e:
                err_msg = str(e)
                if "109429" in err_msg:
                    logger.warning(f"BingX API Rate Limit detected (109429). Backing off for 60s.")
                    self._backoff_until = time.time() + 60
                else:
                    logger.error(f"Error in SignalWorker loop: {e}")
            
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
