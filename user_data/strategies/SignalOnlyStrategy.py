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
        Used to reconcile stoploss_order_id for BingX trigger orders.
        """
        if self.config['exchange']['name'] != 'bingx':
            return

        try:
            from freqtrade.persistence import Trade
            open_trades = Trade.get_trades([Trade.is_open.is_(True)]).all()
            
            for trade in open_trades:
                # If trade is open but has no stoploss_order_id, try to find it on exchange
                if trade.stoploss_order_id is None and trade.stop_loss:
                    try:
                        # Query BingX open trigger orders
                        # We use the internal CCXT instance
                        exchange_name = self.config['exchange']['name']
                        if exchange_name == 'bingx' and hasattr(self.dp.exchange, '_api'):
                            symbol = trade.pair.replace("/", "").replace(":USDT", "USDT")
                            # Fetch open orders from BingX V2 Swap API
                            open_orders = self.dp.exchange._api.swapV2PrivateGetTradeOpenOrders({"symbol": symbol})
                            if open_orders and 'data' in open_orders:
                                for o in open_orders['data']:
                                    # Look for trigger/stop orders on the correct side
                                    # For Long (buy), SL is Sell. For Short (sell), SL is Buy.
                                    order_side = o.get('side', '').lower()
                                    target_side = 'sell' if not trade.is_short else 'buy'
                                    
                                    if order_side == target_side and o.get('type') in ('STOP', 'STOP_MARKET', 'TAKE_PROFIT', 'TAKE_PROFIT_MARKET'):
                                        # Check if price matches our stoploss (within 0.1% tolerance)
                                        o_price = float(o.get('stopPrice') or o.get('price') or 0)
                                        if o_price > 0 and abs(o_price - trade.stop_loss) / trade.stop_loss < 0.001:
                                            logger.info(f"BINGX RECONCILE: Found existing SL order {o['orderId']} for {trade.pair}. Linking.")
                                            trade.stoploss_order_id = str(o['orderId'])
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
