# pragma pylint: disable=missing-docstring
"""Только ручные/форс-входы (Telegram / API). Автоматических сигналов нет."""

from pandas import DataFrame
from datetime import datetime

from freqtrade.strategy import IStrategy
from freqtrade.persistence import Trade


class SignalOnlyStrategy(IStrategy):
    """
    Не выставляет enter_long/enter_short — сделки только через
    /forcelong, /forceshort или REST POST /forceenter (при force_entry_enable).
    Выходы: minimal_roi, stoploss, /forceexit (exit_signal отключён).
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
    # SL-Watchdog (C.4.4): False заставляет бота проверять SL на каждом тике (~5с)
    process_only_new_candles = False
    use_exit_signal = False
    startup_candle_count = 5
    
    # DCA / Position Adjustment (D.6)
    position_adjustment_enable = True
    max_entry_position_adjustment = 3 # До 3-х доборов

    order_types = {
        "entry": "market",            # Вход по рынку (как в сигналах)
        "exit": "limit",             # Выход лимиткой (по цене тейка)
        "stoploss": "market",        # Стоп по рынку
        "stoploss_on_exchange": True, # ВЫСТАВЛЯТЬ СТОП НА БИРЖЕ
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
        signal_sl = trade.get_custom_data("signal_sl")
        if signal_sl is not None:
            sl_price = float(signal_sl)
            if not trade.is_short:
                if sl_price < current_rate:
                    return (sl_price / current_rate) - 1
            else:
                if sl_price > current_rate:
                    return 1 - (sl_price / current_rate)
        
        # Fallback to default stoploss if not specified or already hit
        return self.stoploss

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
