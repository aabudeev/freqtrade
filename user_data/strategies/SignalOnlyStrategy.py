# pragma pylint: disable=missing-docstring
"""Strictly Signal-based Strategy. No automated TA entries."""

from pandas import DataFrame
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)
# Silence noisy Freqtrade core loggers
logging.getLogger('freqtrade.freqtradebot').setLevel(logging.WARNING)
logging.getLogger('freqtrade.worker').setLevel(logging.WARNING)
logging.getLogger('freqtrade.resolvers.strategy_resolver').setLevel(logging.WARNING)

from freqtrade.strategy import IStrategy
from freqtrade.persistence import Trade
from freqtrade.signals.queue_store import SignalQueueStore


class SignalOnlyStrategy(IStrategy):
    """
    Strategy for executing external signals ONLY.
    Entries are made via SignalWorker (Telegram/API).
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.signal_store = SignalQueueStore("/freqtrade/user_data/signals.db")

    INTERFACE_VERSION = 3
    can_short: bool = True

    # Entry/Exit timeouts
    unfilledtimeout = {
        'entry': 10,
        'exit': 525600, # 365 days — TP orders must not be auto-cancelled
        'exit_timeout_count': 0,
        'unit': 'minutes'
    }

    # Reconciliation throttle (5 minutes)
    _last_reconcile_ts = 0
    _reconcile_interval = 300 # seconds

    minimal_roi = {"0": 10.0}  # Effectively disabled
    stoploss = -0.99           # Fallback only
    
    # --- DYNAMIC STOPLOSS ENABLED ---
    use_custom_stoploss = True
    
    # TRAILING STOP DISABLED
    trailing_stop = False
    process_only_new_candles = False
    use_exit_signal = False
    startup_candle_count = 20

    order_types = {
        "entry": "market",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float, side: str,
                 **kwargs) -> float:
        settings = self.signal_store.get_settings()
        lev = float(settings.get('signal_strategy_leverage', 50.0))
        return min(lev, max_leverage)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # No indicators for signal strategy
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Entries only via SignalWorker
        dataframe.loc[:, 'enter_long'] = 0
        dataframe.loc[:, 'enter_short'] = 0
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "exit_long"] = 0
        dataframe.loc[:, "exit_short"] = 0
        return dataframe

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, **kwargs) -> float:
        """
        Force Freqtrade to use our signal-based stop loss.
        Returns the stoploss as a percentage (negative) of current_rate.
        """
        signal_sl_str = trade.get_custom_data("signal_sl")
        if signal_sl_str:
            sl_price = float(signal_sl_str)
            
            # Freqtrade's custom_stoploss expects a ratio relative to current_rate.
            # To keep the absolute sl_price fixed, we must recalculate the ratio
            # based on the current market price (current_rate).
            
            leverage = trade.leverage or 1.0
            if not trade.is_short:
                # LONG: (sl_price / current_rate - 1) * leverage
                ratio = (sl_price / current_rate - 1) * leverage
            else:
                # SHORT: (1 - sl_price / current_rate) * leverage
                ratio = (1 - sl_price / current_rate) * leverage
                
            return ratio

        # Fallback to strategy default
        return self.stoploss

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        """
        Reconcile missing orders on exchange (startup and loop).
        """
        if self.config['exchange']['name'].lower() != 'bingx':
            return

        try:
            from freqtrade.persistence import Trade, Order
            from datetime import datetime
            
            # Use direct CCXT API for reconciliation
            if not (self.dp and hasattr(self.dp, '_exchange') and self.dp._exchange and hasattr(self.dp._exchange, '_api')):
                return
            
            # --- INTERVAL CHECK ---
            # Run reconciliation every 5 minutes to avoid log spam and API rate limits
            now_ts = datetime.now().timestamp()
            last_check = getattr(self, '_last_reconcile_ts', 0)
            if now_ts - last_check < self._reconcile_interval:
                return
            self._last_reconcile_ts = now_ts
            
            api = self.dp._exchange._api
            open_trades = Trade.get_trades([Trade.is_open.is_(True)]).all()
            
            # --- ONE-TIME DATA REPAIR ---
            # Fix trades closed by healer previously that have None in profit fields (causes REST API crash)
            try:
                bad_trades = Trade.get_trades([Trade.is_open.is_(False), Trade.close_profit.is_(None)]).all()
                for bt in bad_trades:
                    logger.warning(f"BINGX RECONCILE: Repairing data for closed trade {bt.id} ({bt.pair})")
                    bt.close_profit = 0.0
                    bt.close_profit_abs = 0.0
                    if bt.close_rate is None:
                        bt.close_rate = bt.open_rate
                if bad_trades:
                    Trade.commit()
            except Exception as e_repair:
                logger.debug(f"BINGX RECONCILE: Repair failed (ignoring): {e_repair}")
                Trade.session.rollback()
            
            # 1. Get all open trades from DB
            open_trades = Trade.get_open_trades()
            if not open_trades:
                return

            logger.info(f"BINGX RECONCILE: Starting aggressive check for {len(open_trades)} trades...")

            # 2. Fetch all positions once to check for orphans
            all_positions = []
            try:
                all_positions = api.fetch_positions()
            except Exception as e_pos:
                logger.error(f"BINGX RECONCILE: Could not fetch positions: {e_pos}")
                return  # SAFETY: If we can't fetch positions, do NOT assume all trades are ghosts

            # SAFETY: If exchange returned zero positions but we have open trades,
            # this is likely an API issue. Do NOT wipe all trades.
            if not all_positions and open_trades:
                logger.warning(f"BINGX RECONCILE: fetch_positions returned EMPTY but we have {len(open_trades)} open trades. Skipping reconciliation (API issue?).")
                return

            # Log what we got for debugging
            pos_symbols = [p.get('symbol', '?') for p in all_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) != 0]
            logger.info(f"BINGX RECONCILE: Active positions on exchange: {pos_symbols}")

            for trade in open_trades:
                # --- SAFETY: Skip trades younger than 10 minutes ---
                trade_age_seconds = (datetime.now(timezone.utc) - trade.open_date_utc).total_seconds() if trade.open_date_utc else 0
                if trade_age_seconds < 600:
                    logger.debug(f"BINGX RECONCILE: Trade {trade.id} ({trade.pair}) is only {trade_age_seconds:.0f}s old, skipping reconciliation.")
                    continue

                # --- AGGRESSIVE BINGX RECONCILE ---
                # Match using CCXT unified symbol format (e.g. "DOGE/USDT:USDT")
                has_position = False
                for p in all_positions:
                    if p.get('symbol') == trade.pair:
                        p_contracts = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                        if p_contracts != 0:
                            has_position = True
                            break
                
                if not has_position:
                    logger.warning(f"BINGX RECONCILE: Trade {trade.id} ({trade.pair}) has NO position on exchange (age: {trade_age_seconds:.0f}s). FORCE CLOSING.")
                    
                    # --- Find ACTUAL close price (critical for accurate statistics) ---
                    actual_close_rate = None
                    
                    # 1. Try to get the real exit price from BingX order history
                    try:
                        closed_orders = api.fetch_closed_orders(trade.pair, limit=20)
                        expected_exit_side = 'sell' if not trade.is_short else 'buy'
                        for co in reversed(closed_orders):  # most recent first
                            if co.get('status') == 'closed' and float(co.get('filled', 0) or 0) > 0:
                                co_side = str(co.get('side', '')).lower()
                                if co_side == expected_exit_side:
                                    price = co.get('average') or co.get('price')
                                    if price and float(price) > 0:
                                        actual_close_rate = float(price)
                                        logger.info(f"BINGX RECONCILE: Found actual close price from order history for {trade.pair}: {actual_close_rate}")
                                        break
                    except Exception as e_hist:
                        logger.debug(f"BINGX RECONCILE: Could not fetch order history for {trade.pair}: {e_hist}")
                    
                    # 2. Fallback: current market price (close to reality)
                    if not actual_close_rate:
                        try:
                            ticker = api.fetch_ticker(trade.pair)
                            actual_close_rate = float(ticker.get('last', 0))
                            if actual_close_rate > 0:
                                logger.info(f"BINGX RECONCILE: Using current market price for {trade.pair}: {actual_close_rate}")
                            else:
                                actual_close_rate = None
                        except Exception as e_tick:
                            logger.debug(f"BINGX RECONCILE: Could not fetch ticker for {trade.pair}: {e_tick}")
                    
                    # 3. Last resort: open_rate (profit = 0, not ideal but won't crash)
                    if not actual_close_rate:
                        actual_close_rate = trade.open_rate
                        logger.warning(f"BINGX RECONCILE: Using open_rate as last resort for {trade.pair} — statistics may be inaccurate!")
                    
                    # Update trade object with real price
                    trade.close_date = datetime.now(timezone.utc)
                    trade.is_open = False
                    trade.exit_reason = "reconciled_missing_position"
                    trade.close_rate = actual_close_rate
                    trade.close_profit = trade.calc_profit_ratio(actual_close_rate)
                    trade.close_profit_abs = trade.calc_profit(actual_close_rate)
                    
                    # Force commit to DB
                    from freqtrade.persistence import Trade
                    Trade.session.add(trade)
                    Trade.session.commit()
                    logger.info(f"BINGX RECONCILE: Trade {trade.id} closed at {actual_close_rate} (profit: {trade.close_profit_abs:.4f} USDT).")
                    continue

                # --- SAFETY CHECK ---
                # Skip trades that are already in the process of exiting to avoid DB conflicts
                if trade.exit_reason or any(o.ft_order_side == trade.exit_side and o.ft_is_open for o in trade.orders):
                    continue

                # Fetch orders using CCXT unified and raw methods
                symbol_api = trade.pair.replace("/", "-").split(":")[0]
                try:
                    # 1. Fetch regular open orders (Limits)

                    # Use the configured api object directly
                    open_orders = api.fetch_open_orders(trade.pair)
                    
                    # 2. Fetch pending/trigger orders (Stops)
                    pending_orders = []
                    
                    # Try raw BingX method which we know exists in this CCXT version
                    try:
                        # Use getattr for maximum safety
                        raw_method = getattr(api, 'swapV2PrivateGetTradePendingOrders', None)
                        if raw_method:
                            resp = raw_method({"symbol": symbol_api})
                            if isinstance(resp, dict) and 'data' in resp:
                                pending_orders = resp['data']
                    except Exception as e_pend:
                        logger.debug(f"BINGX RECONCILE: Pending fallback error: {e_pend}")
                    
                    all_exchange_orders = open_orders + pending_orders

                except Exception as e_fetch:
                    logger.error(f"BINGX RECONCILE: Fetch error for {trade.pair}: {e_fetch}")
                    continue

                # --- RECONCILE TAKE PROFIT (TP) ---
                has_tp = any(o.ft_order_side == trade.exit_side and o.ft_is_open for o in trade.orders)
                if not has_tp:
                    try:
                        tp_order_id = None
                        tp_target = trade.get_custom_data("signal_tp")
                        tp_side = 'SELL' if not trade.is_short else 'BUY'
                        
                        # 3. Scan ALL exchange orders for matches
                        for o in all_exchange_orders:
                            o_side = str(o.get('side', o.get('orderSide', ''))).upper()
                            o_type = str(o.get('type', o.get('orderType', ''))).upper()
                            
                            # BE AGNOSTIC: If it's a SELL order at our TP price, IT IS OUR TP.
                            # Don't strictly check for 'LIMIT' as BingX might call it 'TAKE_PROFIT_LIMIT' etc.
                            if o_side == tp_side:
                                o_price = float(o.get('price') or o.get('stopPrice') or 0)
                                o_amount = float(o.get('amount') or o.get('quantity') or 0)
                                
                                # Check price (within 0.5%) AND amount (within 0.1%)
                                price_match = tp_target and abs(o_price - float(tp_target)) / float(tp_target) < 0.005
                                amount_match = abs(o_amount - trade.amount) / trade.amount < 0.001 if trade.amount > 0 else False
                                
                                if price_match and amount_match:
                                    tp_order_id = str(o.get('id') or o.get('orderId'))
                                    logger.info(f"BINGX RECONCILE: Recognized existing TP {tp_order_id} for {trade.pair} (Type: {o_type}, Amount: {o_amount})")
                                    break
                                elif price_match and not amount_match:
                                    logger.warning(f"BINGX RECONCILE: Found TP for {trade.pair} with WRONG AMOUNT: {o_amount} vs expected {trade.amount}. Will recreate.")
                                    # We don't break here, so we might find a better match or trigger recreate below
                                    pass
                        
                        if tp_order_id:
                            self._register_order(trade, tp_order_id, 'exit', float(tp_target))
                            has_tp = True
                        
                        if not has_tp and tp_target:
                            # Log what we saw to find the "bitch"
                            logger.warning(f"BINGX RECONCILE: Missing TP for {trade.pair}! Saw orders: {[str(o.get('type')) + '@' + str(o.get('price') or o.get('stopPrice')) for o in all_exchange_orders]}")
                            tp_target_rounded = round(float(tp_target), 8)
                            logger.info(f"BINGX RECONCILE: Placing missing TP for {trade.pair} at {tp_target_rounded}")
                            symbol = trade.pair.replace("/", "-").split(":")[0]
                            pos_side = "SHORT" if trade.is_short else "LONG"
                            tp_order = api.swapV2PrivatePostTradeOrder({
                                "symbol": symbol,
                                "side": tp_side.upper(),
                                "positionSide": "BOTH",
                                "type": "LIMIT",
                                "quantity": trade.amount,
                                "price": tp_target_rounded,
                                "reduceOnly": "true"
                            })
                            if tp_order and 'data' in tp_order and isinstance(tp_order['data'], dict):
                                order_id = tp_order['data'].get('orderId')
                                if not order_id and isinstance(tp_order['data'].get('order'), dict):
                                    order_id = tp_order['data']['order'].get('orderId')
                                if order_id:
                                    self._register_order(trade, str(order_id), 'exit', float(tp_target))
                                    has_tp = True
                    except Exception as e_tp:
                        logger.error(f"BINGX RECONCILE: TP Error for {trade.pair}: {e_tp}")

                # --- RECONCILE STOP LOSS (SL) ---
                try:
                    sl_order_id = None
                    has_sl = any(o.ft_order_side == 'stoploss' and o.ft_is_open for o in trade.orders)
                    
                    # Get the 'source of truth' for SL from custom_data
                    signal_sl_str = trade.get_custom_data("signal_sl")
                    sl_price = float(signal_sl_str) if signal_sl_str else trade.stop_loss
                    
                    # If Freqtrade core has overwritten our SL with default strategy SL, fix it back
                    # Use a relative difference check (0.05%) to avoid rounding spam
                    relative_diff = abs(trade.stop_loss - sl_price) / sl_price if sl_price > 0 else 0
                    if signal_sl_str and relative_diff > 0.0005:
                        logger.warning(f"BINGX RECONCILE: Healing corrupted SL for {trade.pair}: {trade.stop_loss} -> {sl_price} (diff: {relative_diff:.4%})")
                        trade.stop_loss = sl_price
                        # Recalculate ratios so FT doesn't try to 'fix' it again
                        leverage = trade.leverage or 1.0
                        sl_ratio = abs((trade.open_rate - sl_price) / trade.open_rate)
                        sl_trade_ratio = sl_ratio * leverage
                        trade.stoploss = -sl_trade_ratio
                        trade.stop_loss_pct = -sl_trade_ratio
                        trade.initial_stop_loss = sl_price
                        trade.initial_stop_loss_pct = -sl_trade_ratio
                        # Use session commit to ensure it hits the disk
                        Trade.session.commit()

                    if not has_sl:
                        target_side = 'SELL' if not trade.is_short else 'BUY'
                        
                        # 3. Scan ALL exchange orders for matches
                        for o in all_exchange_orders:
                            o_side = str(o.get('side', o.get('orderSide', ''))).upper()
                            o_type = str(o.get('type', o.get('orderType', ''))).upper()
                            
                            if o_side == target_side:
                                # Check every possible price field for SL (stopPrice, triggerPrice, etc)
                                o_stop_price = float(o.get('stopPrice') or o.get('triggerPrice') or o.get('stop_price') or o.get('price') or 0)
                                o_amount = float(o.get('amount') or o.get('quantity') or 0)

                                # Check price (within 0.5%) AND amount (within 0.1%)
                                price_match = sl_price and abs(o_stop_price - float(sl_price)) / float(sl_price) < 0.005
                                amount_match = abs(o_amount - trade.amount) / trade.amount < 0.001 if trade.amount > 0 else False

                                if price_match:
                                    # If price matches but amount doesn't, we still recognize it but log a warning
                                    # This handles cases where user has other positions on the same pair (e.g. VST/Demo)
                                    sl_order_id = str(o.get('id') or o.get('orderId'))
                                    if not amount_match:
                                        logger.warning(f"BINGX RECONCILE: Recognized SL {sl_order_id} for {trade.pair} but AMOUNT MISMATCH: {o_amount} vs {trade.amount}. Proceeding anyway.")
                                    else:
                                        logger.info(f"BINGX RECONCILE: Recognized existing SL {sl_order_id} for {trade.pair}")
                                    break
                        
                        if sl_order_id:
                            self._register_order(trade, sl_order_id, 'stoploss', float(sl_price))
                            has_sl = True
                        
                        if not has_sl and sl_price:
                            # Only skip if we found a match above. 
                            # Previous 'non_limit_orders' check was too broad.
                            if not has_sl:
                                sl_price_rounded = round(float(sl_price), 8)
                                logger.info(f"BINGX RECONCILE: Placing missing SL for {trade.pair} at {sl_price_rounded}")
                                symbol = trade.pair.replace("/", "-").split(":")[0]
                                try:
                                    sl_order = api.swapV2PrivatePostTradeOrder({
                                        "symbol": symbol,
                                        "side": target_side.upper(),
                                        "positionSide": "BOTH",
                                        "type": "STOP_MARKET",
                                        "quantity": trade.amount,
                                        "stopPrice": sl_price_rounded,
                                        "reduceOnly": "true"
                                    })
                                    if sl_order and 'data' in sl_order and isinstance(sl_order['data'], dict):
                                        order_id = sl_order['data'].get('orderId')
                                        if order_id:
                                            self._register_order(trade, str(order_id), 'stoploss', float(sl_price))
                                            has_sl = True
                                except Exception as e_place_sl:
                                    logger.error(f"BINGX RECONCILE: Could not place SL for {trade.pair}: {e_place_sl}")
                except Exception as e_sl:
                    logger.error(f"BINGX RECONCILE: SL Error for {trade.pair}: {e_sl}")

        except Exception as e:
            logger.error(f"BINGX RECONCILE: Global error: {e}")

    def _register_order(self, trade: Trade, order_id: str, side: str, price: float) -> None:
        from freqtrade.persistence import Order, Trade
        
        # IDEMPOTENCY CHECK: Don't register if already in trade.orders
        if any(str(o.order_id) == str(order_id) for o in trade.orders):
            return

        # Map 'exit' -> trade.exit_side ('sell' for Long, 'buy' for Short)
        # Freqtrade core only recognizes 'buy', 'sell', 'stoploss' for ft_order_side
        db_side = trade.exit_side if side == 'exit' else side

        new_order = Order(
            ft_trade_id=trade.id,
            ft_pair=trade.pair,
            ft_is_open=True,
            ft_order_side=db_side,
            ft_amount=trade.amount,
            ft_price=price,
            order_id=order_id,
            status='open',
            symbol=trade.pair,
            order_type='limit' if side == 'exit' else 'stoploss',
            side=trade.exit_side,
            amount=trade.amount,
            filled=0.0,
            remaining=trade.amount,
            price=price,
            order_date=datetime.now(timezone.utc)
        )
        trade.orders.append(new_order)
        Trade.commit()
        logger.info(f"BINGX RECONCILE: Registered exchange order {order_id} to DB for {trade.pair}")

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs) -> str | bool | None:
        # We use exchange-side LIMIT orders for TP (managed in bot_loop_start reconciliation)
        # So we don't need manual TP check here anymore. 
        # This prevents market-order conflicts with existing limit orders.
        return None

    def confirm_trade_exit(self, pair: str, trade: Trade, order_type: str, amount: float,
                           rate: float, time_in_force: str, sell_reason: str,
                           current_time: datetime, **kwargs) -> bool:
        logger.info(f"TRADE EXITING: {pair} reason: {sell_reason}")
        return True
