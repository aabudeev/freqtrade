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

            # Fetch all positions once to check for orphans
            all_positions = []
            try:
                all_positions = api.fetch_positions()
            except Exception as e_pos:
                logger.error(f"BINGX RECONCILE: Could not fetch positions: {e_pos}")

            for trade in open_trades:
                # --- SAFETY CHECK ---
                # Skip trades that are already in the process of exiting to avoid DB conflicts
                if trade.exit_reason or any(o.ft_order_side == trade.exit_side and o.ft_is_open for o in trade.orders):
                    continue

                symbol_api = trade.pair.replace("/", "-").split(":")[0]
                
                # --- HEAL ORPHAN TRADES ---
                # Check if this trade has an active position on exchange
                is_orphan = True
                for p in all_positions:
                    p_symbol = p.get('symbol', '').replace(":", "-")
                    p_contracts = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if symbol_api in p_symbol and p_contracts != 0:
                        is_orphan = False
                        break

                # Fetch orders using CCXT unified and raw methods
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
                    
                    # --- PASSIVE MONITORING ---
                    # Only log a warning if position is missing, but NEVER close the trade in DB.
                    if is_orphan and not all_exchange_orders:
                        logger.warning(f"BINGX RECONCILE: Trade {trade.id} ({trade.pair}) appears to have NO position and NO orders on exchange! Check manually.")
                        continue  # Skip TP/SL placement for ghost trades
                    elif is_orphan and all_exchange_orders:
                        logger.debug(f"BINGX RECONCILE: Trade {trade.id} ({trade.pair}) has no position but has orders. Keeping open.")

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

                                if price_match and amount_match:
                                    sl_order_id = str(o.get('id') or o.get('orderId'))
                                    logger.info(f"BINGX RECONCILE: Recognized existing SL {sl_order_id} for {trade.pair} (Type: {o_type}, Amount: {o_amount})")
                                    break
                                elif price_match and not amount_match:
                                    logger.warning(f"BINGX RECONCILE: Found SL for {trade.pair} with WRONG AMOUNT: {o_amount} vs expected {trade.amount}.")
                                    pass
                        
                        if sl_order_id:
                            self._register_order(trade, sl_order_id, 'stoploss', float(sl_price))
                            has_sl = True
                        
                        if not has_sl and sl_price:
                            # CRITICAL: If we see ANY non-limit order, maybe it's our SL but we didn't match it perfectly?
                            # We count them and if any exist, we SKIP placing a new one to be safe.
                            non_limit_orders = [o for o in all_exchange_orders if str(o.get('type', o.get('orderType', ''))).upper() not in ['LIMIT', '']]
                            
                            if non_limit_orders:
                                logger.debug(f"BINGX RECONCILE: Found {len(non_limit_orders)} potential SL/Trigger orders for {trade.pair}. Skipping to avoid duplicates.")
                                has_sl = True
                            
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
