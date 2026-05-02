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
            open_trades = Trade.get_trades([Trade.is_open.is_(True)]).all()
            
            for trade in open_trades:
                # Check if we already have an open stoploss order in our database
                has_sl = any(o.ft_order_side == 'stoploss' and o.ft_is_open for o in trade.orders)
                
                if not has_sl and trade.stop_loss:
                    try:
                        if hasattr(self.dp.exchange, '_api'):
                            symbol = trade.pair.replace("/", "").replace(":USDT", "USDT")
                            # Fetch open orders from BingX V2 Swap API
                            open_orders = self.dp.exchange._api.swapV2PrivateGetTradeOpenOrders({"symbol": symbol})
                            if open_orders and 'data' in open_orders:
                                for o in open_orders['data']:
                                    order_side = o.get('side', '').lower()
                                    target_side = 'sell' if not trade.is_short else 'buy'
                                    
                                    # Identify Stop orders
                                    if order_side == target_side and o.get('type') in ('STOP', 'STOP_MARKET'):
                                        o_price = float(o.get('stopPrice') or o.get('price') or 0)
                                        if o_price > 0 and abs(o_price - trade.stop_loss) / trade.stop_loss < 0.001:
                                            logger.info(f"BINGX RECONCILE: Found existing SL order {o['orderId']} for {trade.pair}. Registering.")
                                            
                                            # Create Order object in database to stop Freqtrade from placing a new one
                                            new_order = Order(
                                                ft_trade_id=trade.id,
                                                ft_pair=trade.pair,
                                                ft_is_open=True,
                                                ft_order_side='stoploss',
                                                order_id=str(o['orderId']),
                                                status='open',
                                                symbol=trade.pair,
                                                order_type='stoploss',
                                                side=target_side,
                                                amount=trade.amount,
                                                filled=0.0,
                                                remaining=trade.amount,
                                                order_date=datetime.now()
                                            )
                                            Trade.session.add(new_order)
                                            Trade.commit()
                                            break
                    except Exception as e_rec:
                        logger.debug(f"BingX SL reconciliation failed for {trade.pair}: {e_rec}")
        except Exception as e:
            logger.error(f"Error in bot_loop_start: {e}")

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
