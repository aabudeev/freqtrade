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
        'exit': 1440, # 24 hours
        'exit_timeout_count': 0,
        'unit': 'minutes'
    }

    # Reconciliation throttle (5 minutes)
    _last_reconcile_ts = 0
    _reconcile_interval = 300 # seconds

    minimal_roi = {"0": 10.0}  # Effectively disabled
    stoploss = -0.99           # Effectively disabled
    
    # TRAILING STOP DISABLED
    trailing_stop = False
    use_custom_stoploss = False
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
            
            # Ensure session is clean to prevent PendingRollbackError loop
            Trade.session.rollback()
            
            # --- INTERVAL CHECK ---
            # Run reconciliation every 5 minutes to avoid log spam and API rate limits
            now_ts = datetime.now().timestamp()
            last_check = getattr(self, '_last_reconcile_ts', 0)
            if now_ts - last_check < self._reconcile_interval:
                return
            self._last_reconcile_ts = now_ts
            
            api = self.dp._exchange._api
            open_trades = Trade.get_trades([Trade.is_open.is_(True)]).all()
            
            for trade in open_trades:
                # Fetch orders using CCXT unified and raw methods
                try:
                    # 1. Fetch regular open orders (Limits)
                    # Use the configured api object directly
                    open_orders = api.fetch_open_orders(trade.pair)
                    
                    # 2. Fetch pending/trigger orders (Stops)
                    pending_orders = []
                    symbol_api = trade.pair.replace("/", "-").split(":")[0]
                    
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
                has_tp = any(o.ft_order_side == 'exit' and o.ft_is_open for o in trade.orders)
                if not has_tp:
                    try:
                        tp_order_id = None
                        tp_target = trade.get_custom_data("signal_tp")
                        tp_side = 'SELL' if not trade.is_short else 'BUY'
                        
                        for o in all_exchange_orders:
                            # Handle both CCXT unified format and BingX raw format
                            o_side = str(o.get('side', '')).upper()
                            o_type = str(o.get('type', '')).upper()
                            
                            if o_type == 'LIMIT' and o_side == tp_side:
                                # CCXT unified price is 'price', Raw is 'price'
                                o_price = float(o.get('price') or 0)
                                if tp_target and abs(o_price - float(tp_target)) / float(tp_target) < 0.005:
                                    tp_order_id = str(o.get('id') or o.get('orderId'))
                                    break
                        
                        if tp_order_id:
                            logger.debug(f"BINGX RECONCILE: Found existing TP {tp_order_id} for {trade.pair}")
                            self._register_order(trade, tp_order_id, 'exit', float(tp_target))
                            has_tp = True
                        
                        if not has_tp and tp_target:
                            logger.info(f"BINGX RECONCILE: Placing missing TP for {trade.pair} at {tp_target}")
                            symbol = trade.pair.replace("/", "-").split(":")[0]
                            tp_order = api.swapV2PrivatePostTradeOrder({
                                "symbol": symbol,
                                "side": tp_side.upper(),
                                "positionSide": "BOTH",
                                "type": "LIMIT",
                                "quantity": trade.amount,
                                "price": tp_target,
                                "reduceOnly": "true"
                            })
                            if tp_order and 'data' in tp_order and isinstance(tp_order['data'], dict):
                                order_id = tp_order['data'].get('orderId')
                                if order_id:
                                    self._register_order(trade, str(order_id), 'exit', float(tp_target))
                                    has_tp = True
                    except Exception as e_tp:
                        logger.error(f"BINGX RECONCILE: TP Error for {trade.pair}: {e_tp}")

                # --- RECONCILE STOP LOSS (SL) ---
                has_sl = any(o.ft_order_side == 'stoploss' and o.ft_is_open for o in trade.orders)
                if not has_sl:
                    try:
                        sl_order_id = None
                        sl_price = trade.stop_loss
                        target_side = 'SELL' if not trade.is_short else 'BUY'
                        
                        for o in all_exchange_orders:
                            o_side = str(o.get('side', o.get('orderSide', ''))).upper()
                            o_type = str(o.get('type', o.get('orderType', ''))).upper()
                            
                            # Log every order for debugging if needed (only if SL not found yet)
                            logger.debug(f"  Checking Order: side={o_side}, type={o_type}, data={o}")
                            
                            # SL is usually NOT a LIMIT order
                            if o_side == target_side and o_type != 'LIMIT':
                                # Check every possible price field
                                prices = [
                                    o.get('stopPrice'), o.get('triggerPrice'), 
                                    o.get('price'), o.get('avgPrice'),
                                    o.get('stop_price'), o.get('trigger_price')
                                ]
                                for p in prices:
                                    try:
                                        if p and abs(float(p) - float(sl_price)) / float(sl_price) < 0.02:
                                            sl_order_id = str(o.get('id') or o.get('orderId'))
                                            break
                                    except (ValueError, TypeError):
                                        continue
                                if sl_order_id:
                                    break
                        
                        if sl_order_id:
                            logger.debug(f"BINGX RECONCILE: Found existing SL {sl_order_id} for {trade.pair}")
                            self._register_order(trade, sl_order_id, 'stoploss', float(sl_price))
                            has_sl = True
                        else:
                            # CRITICAL: If we see ANY non-limit order, maybe it's our SL but we didn't match it perfectly?
                            # We count them and if any exist, we SKIP placing a new one to be safe.
                            non_limit_orders = [o for o in all_exchange_orders if str(o.get('type', o.get('orderType', ''))).upper() != 'LIMIT']
                            if non_limit_orders:
                                logger.debug(f"BINGX RECONCILE: Found {len(non_limit_orders)} potential SL orders for {trade.pair}. Skipping to avoid duplicates.")
                                has_sl = True
                        
                        if not has_sl and sl_price:
                            sl_price_rounded = round(float(sl_price), 8)
                            logger.info(f"BINGX RECONCILE: Placing missing SL for {trade.pair} at {sl_price_rounded}")
                            symbol = trade.pair.replace("/", "-").split(":")[0]
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
                    except Exception as e_sl:
                        logger.error(f"BINGX RECONCILE: SL Error for {trade.pair}: {e_sl}")

        except Exception as e:
            logger.error(f"BINGX RECONCILE: Global error: {e}")

    def _register_order(self, trade, order_id, side, price):
        from freqtrade.persistence import Order, Trade
        new_order = Order(
            ft_trade_id=trade.id,
            ft_pair=trade.pair,
            ft_is_open=True,
            ft_order_side=side,
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
