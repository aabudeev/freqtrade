# pragma pylint: disable=missing-docstring
"""Manual/Force Entry Only Strategy (Telegram / API). No automated signals."""

from pandas import DataFrame
from datetime import datetime

from freqtrade.strategy import IStrategy
from freqtrade.persistence import Trade


class SignalOnlyStrategy(IStrategy):
    """
    Strategy for executing external signals.
    Does not generate entry signals. Entries are made via /forcelong, /forceshort, or SignalWorker.
    Exits: minimal_roi, stoploss, /forceexit, and custom SL/TP logic.
    """

    INTERFACE_VERSION = 3

    can_short: bool = False

    minimal_roi = {"0": 10.0}  # 1000% profit (effectively disabled)
    stoploss = -0.99           # 99% loss (effectively disabled, using custom_stoploss instead)
    
    # Trailing TP (C.4.3)
    trailing_stop = True
    trailing_stop_positive = 0.01
    trailing_stop_positive_offset = 0.02
    trailing_only_offset_is_reached = True

    timeframe = "5m"
    # SL-Watchdog (C.4.4): Set to False to check SL on every tick (~5s) instead of just on new candles
    process_only_new_candles = False
    use_exit_signal = False
    startup_candle_count = 5
    
    # DCA / Position Adjustment (D.6)
    position_adjustment_enable = True
    max_entry_position_adjustment = 3 # Up to 3 additional entries

    order_types = {
        "entry": "market",            # Market entry (as in signals)
        "exit": "limit",              # Limit exit (at target price)
        "stoploss": "market",         # Market stop-loss
        "stoploss_on_exchange": True, # PLACE STOP-LOSS ON THE EXCHANGE
    }
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    plot_config = {
        "main_plot": {
            "ema20": {"color": "#e0752f"},
            "ema50": {"color": "#2196f3"},
        },
        "subplots": {
            "RSI": {
                "rsi": {"color": "#9c27b0"},
            }
        },
    }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        import talib.abstract as ta
        dataframe['ema20'] = ta.EMA(dataframe, timeperiod=20)
        dataframe['ema50'] = ta.EMA(dataframe, timeperiod=50)
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "enter_long"] = 0
        dataframe.loc[:, "enter_short"] = 0
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "exit_long"] = 0
        dataframe.loc[:, "exit_short"] = 0
        return dataframe

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, **kwargs) -> float:
        # Get stop-loss price from signal custom data
        signal_sl = trade.get_custom_data("signal_sl")
        if signal_sl is not None:
            sl_price = float(signal_sl)
            if not trade.is_short:
                if sl_price < current_rate:
                    return (sl_price / current_rate) - 1
            else:
                if sl_price > current_rate:
                    return 1 - (sl_price / current_rate)
        
        # Fallback to default stoploss if not specified
        return self.stoploss

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs) -> str | bool | None:
        # Get take-profit price from signal custom data
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
