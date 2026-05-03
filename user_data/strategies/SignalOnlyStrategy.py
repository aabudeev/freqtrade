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

    def bot_loop_start(self, **kwargs) -> None:
        """
        Called at the start of each bot iteration.
        Used to reconcile stoploss orders for BingX (Freqtrade V3 schema).
        """
        if self.config['exchange']['name'] != 'bingx':
            return

        try:
            from freqtrade.persistence import Trade, Order
            from datetime import datetime, timezone, timedelta
            open_trades = Trade.get_trades([Trade.is_open.is_(True)]).all()
            
            for trade in open_trades:
                # No more 30s delay - worker is now passive regarding orders.
                # Only strategy handles order placement and registration.

                # 1. Stop-Loss (SL) Reconciliation
                has_sl = any(o.ft_order_side == 'stoploss' and o.ft_is_open for o in trade.orders)
                
                # 2. Take-Profit (TP) Reconciliation
                has_tp = any(o.ft_order_side == 'exit' and o.ft_is_open for o in trade.orders)
                
                # Fetch open orders from BingX to see what's actually there
                try:
                    if hasattr(self.dp.exchange, '_api'):
                        symbol = trade.pair.replace("/", "").replace(":USDT", "USDT")
                        open_orders_raw = self.dp.exchange._api.swapV2PrivateGetTradeOpenOrders({"symbol": symbol})
                        
                        if open_orders_raw and 'data' in open_orders_raw:
                            sl_order_id = None
                            tp_order_id = None
                            
                            tp_target = trade.get_custom_data("signal_tp")
                            
                            for o in open_orders_raw['data']:
                                order_side = o.get('side', '').lower()
                                target_side = 'sell' if not trade.is_short else 'buy'
                                
                                # Check for SL (Stop/Stop Market)
                                if not has_sl and order_side == target_side and o.get('type') in ('STOP', 'STOP_MARKET'):
                                    o_price = float(o.get('stopPrice') or o.get('price') or 0)
                                    if trade.stop_loss and abs(o_price - trade.stop_loss) / trade.stop_loss < 0.001:
                                        sl_order_id = str(o['orderId'])
                                
                                # Check for TP (Limit order at signal_tp price)
                                if not has_tp and order_side == target_side and o.get('type') == 'LIMIT':
                                    o_price = float(o.get('price') or 0)
                                    if tp_target and abs(o_price - float(tp_target)) / float(tp_target) < 0.001:
                                        tp_order_id = str(o['orderId'])

                            # Register SL if found on exchange but missing in DB
                            if sl_order_id and not has_sl:
                                logger.info(f"BINGX RECONCILE: Registering existing SL {sl_order_id} for {trade.pair}")
                                self._register_order(trade, sl_order_id, 'stoploss', trade.stop_loss)
                                has_sl = True
                            
                            # Register TP if found on exchange but missing in DB
                            if tp_order_id and not has_tp:
                                logger.info(f"BINGX RECONCILE: Registering existing TP {tp_order_id} for {trade.pair}")
                                self._register_order(trade, tp_order_id, 'exit', float(tp_target))
                                has_tp = True

                        # 3. Placement: If TP is missing both in DB and on Exchange, place it
                        if not has_tp:
                            tp_price_str = trade.get_custom_data("signal_tp")
                            if tp_price_str:
                                tp_price = float(tp_price_str)
                                try:
                                    logger.info(f"BINGX RECONCILE: Placing missing TP for {trade.pair} at {tp_price}")
                                    tp_order = self.dp.exchange._api.create_order(
                                        symbol=trade.pair,
                                        type="limit",
                                        side=trade.exit_side,
                                        amount=trade.amount,
                                        price=tp_price,
                                        params={"reduceOnly": True}
                                    )
                                    self._register_order(trade, str(tp_order['id']), 'exit', tp_price)
                                    logger.info(f"BINGX RECONCILE: TP placed for {trade.pair}")
                                except Exception as e_tp:
                                    logger.error(f"Failed to place missing TP for {trade.pair}: {e_tp}")

                except Exception as e_api:
                    logger.debug(f"BingX API check failed for {trade.pair}: {e_api}")

        except Exception as e:
            logger.error(f"Error in bot_loop_start: {e}")

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
