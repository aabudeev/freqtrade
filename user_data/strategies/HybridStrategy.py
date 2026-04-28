# pragma pylint: disable=missing-docstring
"""Hybrid Strategy. Signals confirmed by Indicators."""

from pandas import DataFrame
from datetime import datetime
import logging
import pandas_ta as ta
import numpy as np

logger = logging.getLogger(__name__)

from freqtrade.strategy import IStrategy
from freqtrade.persistence import Trade
from freqtrade.signals.queue_store import SignalQueueStore
from freqtrade.signals.parser import parse_signal_text


class HybridStrategy(IStrategy):
    """
    Strategy that requires BOTH an external signal AND indicator confirmation.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.signal_store = SignalQueueStore("/freqtrade/user_data/signals.db")

    INTERFACE_VERSION = 3
    can_short: bool = True

    minimal_roi = {"0": 10.0}
    stoploss = -0.99
    
    trailing_stop = True
    trailing_stop_positive = 0.01
    trailing_stop_positive_offset = 0.02
    trailing_only_offset_is_reached = True

    timeframe = "5m"
    use_custom_stoploss = True
    process_only_new_candles = False
    use_exit_signal = False
    startup_candle_count = 200

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
        lev = float(settings.get('hybrid_strategy_leverage', 20.0))
        return min(lev, max_leverage)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if len(dataframe) < 20:
            return dataframe

        dataframe['ema50'] = ta.ema(dataframe['close'], length=50)
        dataframe['rsi'] = ta.rsi(dataframe['close'], length=14)

        st = ta.supertrend(dataframe['high'], dataframe['low'], dataframe['close'], length=10, multiplier=3)
        if st is not None and not st.empty:
            dataframe['supertrend_direction'] = st.iloc[:, 1]

        # Order Blocks
        dataframe['order_block_low'] = np.nan
        dataframe['order_block_high'] = np.nan
        for i in range(5, len(dataframe)):
            if dataframe['low'].iloc[i-3] == dataframe['low'].iloc[i-5:i].min():
                dataframe.loc[dataframe.index[i:], 'order_block_low'] = dataframe['low'].iloc[i-3]
                dataframe.loc[dataframe.index[i:], 'order_block_high'] = dataframe['high'].iloc[i-3]
            if dataframe['high'].iloc[i-3] == dataframe['high'].iloc[i-5:i].max():
                dataframe.loc[dataframe.index[i:], 'order_block_supply_high'] = dataframe['high'].iloc[i-3]
                dataframe.loc[dataframe.index[i:], 'order_block_supply_low'] = dataframe['low'].iloc[i-3]

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, 'enter_long'] = 0
        dataframe.loc[:, 'enter_short'] = 0

        # 1. Calculate indicator confirmation
        indicator_long = (
            (dataframe['close'] > dataframe['ema50']) &
            (dataframe['supertrend_direction'] == 1) &
            (dataframe['rsi'] > 40) & (dataframe['rsi'] < 70) &
            (dataframe['close'] <= dataframe['order_block_high']) &
            (dataframe['close'] >= dataframe['order_block_low'])
        )
        
        indicator_short = (
            (dataframe['close'] < dataframe['ema50']) &
            (dataframe['supertrend_direction'] == -1) &
            (dataframe['rsi'] < 60) & (dataframe['rsi'] > 30) &
            (dataframe.get('order_block_supply_high') is not None) &
            (dataframe['close'] >= dataframe.get('order_block_supply_low', 0)) &
            (dataframe['close'] <= dataframe.get('order_block_supply_high', 0))
        )

        # 2. Check for signals
        waiting = self.signal_store.get_waiting_signals()
        for sig in waiting:
            symbol = sig.get('symbol')
            if symbol and (symbol == metadata['pair'] or symbol.split(':')[0] == metadata['pair'].split(':')[0]):
                event = parse_signal_text(sig['text'])
                if event:
                    is_long = (event.side.name == 'LONG')
                    is_short = (event.side.name == 'SHORT')
                    
                    if is_long and indicator_long.iloc[-1]:
                        dataframe.loc[dataframe.index[-1], 'enter_long'] = 1
                        dataframe.loc[dataframe.index[-1], 'enter_tag'] = f"hybrid_{sig['idempotency_key']}"
                    elif is_short and indicator_short.iloc[-1]:
                        dataframe.loc[dataframe.index[-1], 'enter_short'] = 1
                        dataframe.loc[dataframe.index[-1], 'enter_tag'] = f"hybrid_{sig['idempotency_key']}"

        return dataframe

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                            time_in_force: str, current_time: datetime, entry_tag: str | None,
                            side: str, **kwargs) -> bool:
        if entry_tag and entry_tag.startswith("hybrid_"):
            key = entry_tag.replace("hybrid_", "")
            self.signal_store.mark_status(key, "sent")
            logger.info(f"Hybrid trade confirmed for {pair}. Signal {key} marked as sent.")
        return True

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "exit_long"] = 0
        dataframe.loc[:, "exit_short"] = 0
        return dataframe

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, **kwargs) -> float:
        signal_sl = trade.get_custom_data("signal_sl")
        if signal_sl is not None:
            sl_price = float(signal_sl)
            if not trade.is_short:
                if sl_price < current_rate:
                    return (sl_price / current_rate) - 1
            else:
                if sl_price > current_rate:
                    return 1 - (sl_price / current_rate)
        
        lev = trade.leverage if trade.leverage else 1.0
        safety_sl = (0.8 / lev)
        return -safety_sl

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs) -> str | bool | None:
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
