"""
Signal-only strategy with corrected stoploss handling.
"""

from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.enums import ExitType, SignalDirection, TradeDirection
from freqtrade.exceptions import OperationalException
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, IntParameter, DecimalParameter, CategoricalParameter


class SignalOnlyStrategy(IStrategy):
    """
    Signal-only strategy with proper stoploss handling.
    """

    # ROI table:
    minimal_roi = {
        "0": 0.05,  # 5% if held for less than 1 hour
        "60": 0.02,  # 2% if held for 1 hour or more
        "120": 0.01,  # 1% if held for 2 hours or more
        "180": 0.005,  # 0.5% if held for 3 hours or more
        "240": 0,  # Stop loss if held for 4 hours or more
    }

    # Stoploss
    stoploss = -0.15  # 15% stoploss

    # Trailing stop
    trailing_stop = False
    trailing_stop_positive = 0.001  # 0.1%
    trailing_stop_positive_offset = 0.002  # 0.2%
    trailing_only_offset_as_percent = False

    # Order types
    order_types = {
        'entry': 'market',
        'exit': 'market',
        'stoploss': 'market',
        'stoploss_on_exchange': True,
        'stoploss_on_exchange_interval': 60,
    }

    # Leverage
    leverage_option = CategoricalParameter([1, 2, 5, 10, 25], default=1, space='buy', optimize=False)
    short_leverage_option = CategoricalParameter([1, 2, 5, 10, 25], default=1, space='sell', optimize=False)

    # Custom parameters
    use_signal_stoploss = True

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # No indicators needed for signal-only strategy
        dataframe['enter_long'] = 0
        dataframe['enter_short'] = 0
        dataframe['exit_long'] = 0
        dataframe['exit_short'] = 0
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
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
        Strictly use the stoploss price set during entry.
        No trailing, no adjustments.
        """
        # Use stoploss from signal
        signal_sl = trade.get_custom_data("signal_sl")
        if signal_sl is not None:
            sl_price = float(signal_sl)
            if not trade.is_short:
                if sl_price < current_rate:
                    return (sl_price / current_rate) - 1
            else:
                if sl_price > current_rate:
                    return 1 - (sl_price / current_rate)
        
        # Fallback to strategy stoploss if signal data is missing
        return self.stoploss

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs) -> str | bool | None:
        # Take profit from signal
        signal_tp = trade.get_custom_data("signal_tp")
        if signal_tp is not None:
            tp_price = float(signal_tp)
            if not trade.is_short:
                if current_rate >= tp_price:
                    return "signal_tp"
            else:
                if current_rate <= tp_price:
                    return "signal_tp"
        return None

    def leverages(self) -> Tuple[List[Optional[int]], List[Optional[int]]]:
        """Return leverage settings for long and short positions."""
        return [self.leverage_option.value], [self.short_leverage_option.value]

    def get_signal_direction(self, pair: str, trade: Trade) -> Optional[SignalDirection]:
        """Return signal direction based on trade properties."""
        try:
            if trade.is_short:
                return SignalDirection.SHORT
            return SignalDirection.LONG
        except Exception:
            return None

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Signal-only strategy - no entry indicators
        dataframe.loc[:, 'enter_long'] = 0
        dataframe.loc[:, 'enter_short'] = 0
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Signal-only strategy - no exit indicators
        dataframe.loc[:, "exit_long"] = 0
        dataframe.loc[:, "exit_short"] = 0
        return dataframe