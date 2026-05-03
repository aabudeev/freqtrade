# pragma pylint: disable=missing-docstring
"""Strictly Signal-based Strategy. No automated TA entries."""

from pandas import DataFrame
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)

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
            
            api = self.dp._exchange._api
            open_trades = Trade.get_trades([Trade.is_open.is_(True)]).all()
            
            for trade in open_trades:
                # --- RECONCILE TAKE PROFIT (TP) ---
                has_tp = any(o.ft_order_side == 'exit' and o.ft_is_open for o in trade.orders)
                
                if not has_tp:
                    try:
                        open_orders = self.dp._exchange._api.fetch_open_orders(trade.pair)
                        
                        tp_order_id = None
                        tp_target = trade.get_custom_data("signal_tp")
                        target_side = 'sell' if not trade.is_short else 'buy'
                        
                        for o in open_orders:
                            # Check for existing limit order at TP price
                            if o.get('side') == target_side and o.get('type') == 'limit':
                                o_price = float(o.get('price') or 0)
                                if tp_target and abs(o_price - float(tp_target)) / float(tp_target) < 0.001:
                                    tp_order_id = str(o['id'])
                                    break
                        
                        if tp_order_id:
                            logger.info(f"BINGX RECONCILE: Found existing TP {tp_order_id} for {trade.pair}")
                            self._register_order(trade, tp_order_id, 'exit', float(tp_target))
                            has_tp = True
                        
                        # Cleanup invalid orders
                        for o in list(trade.orders):
                            if o.ft_is_open and (o.order_id is None or str(o.order_id).lower() == 'none'):
                                o.ft_is_open = False
                                o.status = 'cancelled'
                        Trade.commit()
                        
                        if not has_tp and tp_target:
                            tp_price = float(tp_target)
                            logger.info(f"BINGX RECONCILE: Placing missing TP for {trade.pair} at {tp_price}")
                            symbol = trade.pair.replace("/", "-").split(":")[0]
                            tp_order = api.swapV2PrivatePostTradeOrder({
                                "symbol": symbol,
                                "side": target_side.upper(),
                                "positionSide": "BOTH",
                                "type": "LIMIT",
                                "quantity": trade.amount,
                                "price": tp_price,
                                "reduceOnly": "true"
                            })
                            if tp_order and 'data' in tp_order and isinstance(tp_order['data'], dict):
                                new_id = str(tp_order['data'].get('orderId'))
                                if new_id and new_id != 'None':
                                    self._register_order(trade, new_id, 'exit', tp_price)
                                    logger.info(f"BINGX RECONCILE: TP placed for {trade.pair}, orderId: {new_id}")

                    except Exception as e_tp:
                        logger.error(f"BINGX RECONCILE: TP Error for {trade.pair}: {e_tp}")

                # --- RECONCILE STOP LOSS (SL) ---
                # Freqtrade usually marks SL orders with o.ft_order_side == 'stoploss'
                has_sl = any(o.ft_order_side == 'stoploss' and o.ft_is_open for o in trade.orders)
                
                if not has_sl:
                    try:
                        open_orders = self.dp._exchange._api.fetch_open_orders(trade.pair)
                        sl_order_id = None
                        sl_price = trade.stop_loss
                        target_side = 'sell' if not trade.is_short else 'buy'
                        
                        for o in open_orders:
                            # BingX Stop Market orders might have type 'STOP_MARKET' or 'TRIGGER_MARKET'
                            o_type = str(o.get('type', '')).upper()
                            if o.get('side') == target_side and ('STOP' in o_type or 'TRIGGER' in o_type):
                                o_stop_price = float(o.get('stopPrice') or o.get('price') or 0)
                                if sl_price and abs(o_stop_price - float(sl_price)) / float(sl_price) < 0.01:
                                    sl_order_id = str(o['id'])
                                    break
                        
                        if sl_order_id:
                            logger.info(f"BINGX RECONCILE: Found existing SL {sl_order_id} for {trade.pair}")
                            self._register_order(trade, sl_order_id, 'stoploss', float(sl_price))
                            has_sl = True
                        
                        if not has_sl and sl_price:
                            logger.info(f"BINGX RECONCILE: Placing missing SL for {trade.pair} at {sl_price}")
                            symbol = trade.pair.replace("/", "-").split(":")[0]
                            sl_order = api.swapV2PrivatePostTradeOrder({
                                "symbol": symbol,
                                "side": target_side.upper(),
                                "positionSide": "BOTH",
                                "type": "STOP_MARKET",
                                "quantity": trade.amount,
                                "stopPrice": sl_price,
                                "reduceOnly": "true"
                            })
                            if sl_order and 'data' in sl_order and isinstance(sl_order['data'], dict):
                                new_id = str(sl_order['data'].get('orderId'))
                                if new_id and new_id != 'None':
                                    self._register_order(trade, new_id, 'stoploss', sl_price)
                                    logger.info(f"BINGX RECONCILE: SL placed for {trade.pair}, orderId: {new_id}")
                                    
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
        # Take profit from signal
        signal_tp = trade.get_custom_data("signal_tp")
        if signal_tp is not None:
            tp_price = float(signal_tp)
            if not trade.is_short:
                if current_rate >= tp_price:
                    return f"signal_tp_{tp_price}"
            else:
                if current_rate <= tp_price:
                    return f"signal_tp_{tp_price}"
        return None
