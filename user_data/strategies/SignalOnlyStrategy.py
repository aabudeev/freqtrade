# pragma pylint: disable=missing-docstring
"""Strictly Signal-based Strategy. No automated TA entries."""

from pandas import DataFrame
from datetime import datetime
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
        "stoploss_on_exchange": True,
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
            
            api = self.dp._exchange._api
            open_trades = Trade.get_trades([Trade.is_open.is_(True)]).all()
            
            for trade in open_trades:
                # 1. Check if we already have an open exit order in DB
                has_tp = any(o.ft_order_side == 'exit' and o.ft_is_open for o in trade.orders)
                
                if not has_tp:
                    # 2. Check exchange for existing TP
                    try:
                        # Correct BingX V2 symbol: AVAX-USDT
                        api_symbol = trade.pair.replace("/", "-").split(":")[0]
                        open_orders_raw = api.swapV2PrivateGetTradeOpenOrders({"symbol": api_symbol})
                        
                        if open_orders_raw and 'data' in open_orders_raw:
                            tp_order_id = None
                            tp_target = trade.get_custom_data("signal_tp")
                            
                            target_side = 'sell' if not trade.is_short else 'buy'
                            
                            for o in open_orders_raw['data']:
                                if o.get('side', '').lower() == target_side and o.get('type') == 'LIMIT':
                                    o_price = float(o.get('price') or 0)
                                    if tp_target and abs(o_price - float(tp_target)) / float(tp_target) < 0.001:
                                        tp_order_id = str(o['orderId'])
                                        break
                            
                            if tp_order_id:
                                logger.info(f"BINGX RECONCILE: Found existing TP {tp_order_id} for {trade.pair}")
                                self._register_order(trade, tp_order_id, 'exit', float(tp_target))
                                has_tp = True
                        
                        # 3. If still no TP, place it
                        if not has_tp:
                            tp_price_str = trade.get_custom_data("signal_tp")
                            if tp_price_str:
                                tp_price = float(tp_price_str)
                                logger.info(f"BINGX RECONCILE: Placing missing TP for {trade.pair} at {tp_price}")
                                
                                tp_order = api.swapV2PrivatePostTradeOrder({
                                    "symbol": api_symbol,
                                    "side": trade.exit_side.upper(),
                                    "positionSide": "LONG" if not trade.is_short else "SHORT",
                                    "type": "LIMIT",
                                    "quantity": trade.amount,
                                    "price": tp_price,
                                    "reduceOnly": "true"
                                })
                                
                                if tp_order and 'data' in tp_order:
                                    new_id = str(tp_order['data'].get('orderId'))
                                    self._register_order(trade, new_id, 'exit', tp_price)
                                    logger.info(f"BINGX RECONCILE: TP placed for {trade.pair}, orderId: {new_id}")

                    except Exception as e_inner:
                        logger.error(f"BINGX RECONCILE: Error for {trade.pair}: {e_inner}")

        except Exception as e:
            logger.error(f"BINGX RECONCILE: Global error: {e}")

    def _register_order(self, trade, order_id, side, price):
        from freqtrade.persistence import Order, Trade
        new_order = Order(
            ft_trade_id=trade.id,
            ft_pair=trade.pair,
            ft_is_open=True,
            ft_order_side=side,
            order_id=order_id,
            status='open',
            symbol=trade.pair,
            order_type='limit' if side == 'exit' else 'stoploss',
            side=trade.exit_side,
            amount=trade.amount,
            filled=0.0,
            remaining=trade.amount,
            price=price,
            order_date=datetime.now()
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
