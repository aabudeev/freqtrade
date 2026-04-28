# pragma pylint: disable=missing-docstring
"""Strictly Indicator-based Strategy. No external signals."""

from pandas import DataFrame
from datetime import datetime
import logging
import pandas_ta as ta
import numpy as np

logger = logging.getLogger(__name__)

from freqtrade.strategy import IStrategy
from freqtrade.persistence import Trade
from freqtrade.signals.queue_store import SignalQueueStore


class IndicatorOnlyStrategy(IStrategy):
    """
    Strategy for automated trading based on Technical Analysis indicators ONLY.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.signal_store = SignalQueueStore("/freqtrade/user_data/signals.db")

    INTERFACE_VERSION = 3
    can_short: bool = True

    minimal_roi = {"0": 0.1}   # 10%
    stoploss = -0.10           # 10% default

    timeframe = "5m"
    use_custom_stoploss = True
    process_only_new_candles = True
    use_exit_signal = True
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
        lev = float(settings.get('indicator_strategy_leverage', 10.0))
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

        dataframe.loc[
            (dataframe['close'] > dataframe['ema50']) &
            (dataframe['supertrend_direction'] == 1) &
            (dataframe['rsi'] > 40) & (dataframe['rsi'] < 70) &
            (dataframe['close'] <= dataframe['order_block_high']) &
            (dataframe['close'] >= dataframe['order_block_low']),
            'enter_long'
        ] = 1
        
        dataframe.loc[
            (dataframe['close'] < dataframe['ema50']) &
            (dataframe['supertrend_direction'] == -1) &
            (dataframe['rsi'] < 60) & (dataframe['rsi'] > 30) &
            (dataframe.get('order_block_supply_high') is not None) &
            (dataframe['close'] >= dataframe.get('order_block_supply_low', 0)) &
            (dataframe['close'] <= dataframe.get('order_block_supply_high', 0)),
            'enter_short'
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "exit_long"] = 0
        dataframe.loc[:, "exit_short"] = 0
        dataframe.loc[(dataframe['rsi'] > 75), 'exit_long'] = 1
        dataframe.loc[(dataframe['rsi'] < 25), 'exit_short'] = 1
        return dataframe

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, **kwargs) -> float:
        lev = trade.leverage if trade.leverage else 1.0
        safety_sl = (0.8 / lev)
        return -safety_sl
