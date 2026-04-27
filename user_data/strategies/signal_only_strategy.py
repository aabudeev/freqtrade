# pragma pylint: disable=missing-docstring
"""Manual/Force Entry Only Strategy (Telegram / API). No automated signals."""

from pandas import DataFrame
from datetime import datetime
import logging
import pandas_ta as ta
import numpy as np

logger = logging.getLogger(__name__)

from freqtrade.strategy import IStrategy, merge_informative_pair
from freqtrade.persistence import Trade
from freqtrade.signals.queue_store import SignalQueueStore
from freqtrade.signals.parser import parse_signal_text


class SignalOnlyStrategy(IStrategy):
    """
    Strategy for executing external signals.
    Does not generate entry signals. Entries are made via /forcelong, /forceshort, or SignalWorker.
    Exits: minimal_roi, stoploss, /forceexit, and custom SL/TP logic.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.signal_store = SignalQueueStore("/freqtrade/user_data/signals.db")

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
    startup_candle_count = 200
    
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
            "bb_lowerband": {"color": "rgba(255,255,255,0.1)", "fill_to": "bb_upperband"},
            "bb_middleband": {"color": "rgba(255,255,255,0.2)"},
            "supertrend": {"color": "#ffff00"},
            "order_block_low": {"color": "#00ff00", "fill_to": "order_block_high"},
            "order_block_high": {"color": "rgba(0,255,0,0.1)"},
        },
        "subplots": {
            "RSI": {
                "rsi": {"color": "#9c27b0"},
            },
            "MACD": {
                "macd": {"color": "#2196f3"},
                "macdsignal": {"color": "#ff9800"},
            }
        },
    }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Check if we have enough data
        if len(dataframe) < 20:
            return dataframe

        # 1. EMAs (Trend)
        dataframe['ema20'] = ta.ema(dataframe['close'], length=20)
        dataframe['ema50'] = ta.ema(dataframe['close'], length=50)
        dataframe['ema200'] = ta.ema(dataframe['close'], length=200)

        # 2. RSI (Momentum)
        dataframe['rsi'] = ta.rsi(dataframe['close'], length=14)

        # 3. Bollinger Bands (Volatility)
        bb = ta.bbands(dataframe['close'], length=20, std=2)
        if bb is not None and not bb.empty:
            dataframe['bb_lowerband'] = bb.iloc[:, 0]
            dataframe['bb_middleband'] = bb.iloc[:, 1]
            dataframe['bb_upperband'] = bb.iloc[:, 2]

        # 4. MACD (Confirmation)
        macd = ta.macd(dataframe['close'], fast=12, slow=26, signal=9)
        if macd is not None and not macd.empty:
            dataframe['macd'] = macd.iloc[:, 0]
            dataframe['macdsignal'] = macd.iloc[:, 2] # Usually index 2 is signal

        # 5. SuperTrend (Volatility Trend)
        st = ta.supertrend(dataframe['high'], dataframe['low'], dataframe['close'], length=10, multiplier=3)
        if st is not None and not st.empty:
            dataframe['supertrend'] = st.iloc[:, 0]
            dataframe['supertrend_direction'] = st.iloc[:, 1]

        # 6. Order Blocks (SMC Lite - Support/Resistance Zones)
        # We find pivots (local high/low) and carry them forward
        dataframe['order_block_low'] = np.nan
        dataframe['order_block_high'] = np.nan
        
        # Simple pivot detection (window of 5 candles)
        for i in range(5, len(dataframe)):
            # Bullish OB (Demand): a low pivot followed by a strong move up
            if dataframe['low'].iloc[i-3] == dataframe['low'].iloc[i-5:i].min():
                dataframe.loc[dataframe.index[i:], 'order_block_low'] = dataframe['low'].iloc[i-3]
                dataframe.loc[dataframe.index[i:], 'order_block_high'] = dataframe['high'].iloc[i-3]
                
            # Bearish OB (Supply): a high pivot followed by a strong move down
            if dataframe['high'].iloc[i-3] == dataframe['high'].iloc[i-5:i].max():
                dataframe.loc[dataframe.index[i:], 'order_block_supply_high'] = dataframe['high'].iloc[i-3]
                dataframe.loc[dataframe.index[i:], 'order_block_supply_low'] = dataframe['low'].iloc[i-3]

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Get strategy mode from DB settings to allow dynamic switching
        settings = self.signal_store.get_settings()
        strategy_mode = settings.get('strategy_mode', 'signal')
        
        # Log mode occasionally (once per pair per 15 mins to avoid spam)
        if not hasattr(self, '_last_log'): self._last_log = {}
        now = datetime.now().timestamp()
        if now - self._last_log.get(metadata['pair'], 0) > 900:
            logger.info(f"Strategy mode for {metadata['pair']}: {strategy_mode.upper()}")
            self._last_log[metadata['pair']] = now
        
        dataframe.loc[:, 'enter_long'] = 0
        dataframe.loc[:, 'enter_short'] = 0

        # 1. INDICATOR MODE (Automated TA)
        if strategy_mode in ['indicator', 'hybrid']:
            # Strong Bullish Conditions:
            # - Price above EMA50
            # - SuperTrend is Green (1)
            # - RSI is in healthy range (45-65)
            # - Price is INSIDE or touching a Bullish Order Block (Demand zone)
            dataframe.loc[
                (dataframe['close'] > dataframe['ema50']) &
                (dataframe['supertrend_direction'] == 1) &
                (dataframe['rsi'] > 40) & (dataframe['rsi'] < 70) &
                (dataframe['close'] <= dataframe['order_block_high']) &
                (dataframe['close'] >= dataframe['order_block_low']),
                'enter_long'
            ] = 1
            
            # Strong Bearish Conditions:
            dataframe.loc[
                (dataframe['close'] < dataframe['ema50']) &
                (dataframe['supertrend_direction'] == -1) &
                (dataframe['rsi'] < 60) & (dataframe['rsi'] > 30) &
                (dataframe.get('order_block_supply_high') is not None) &
                (dataframe['close'] >= dataframe.get('order_block_supply_low', 0)) &
                (dataframe['close'] <= dataframe.get('order_block_supply_high', 0)),
                'enter_short'
            ] = 1

        # 2. SIGNAL / HYBRID MODE
        # Check for waiting signals from SignalWorker
        waiting = self.signal_store.get_waiting_signals()
        for sig in waiting:
            symbol = sig.get('symbol')
            # Handle symbol matching (e.g. BTC/USDT:USDT)
            if symbol and (symbol == metadata['pair'] or symbol.split(':')[0] == metadata['pair'].split(':')[0]):
                event = parse_signal_text(sig['text'])
                if event:
                    is_long = (event.side.name == 'LONG')
                    is_short = (event.side.name == 'SHORT')
                    
                    if strategy_mode == 'hybrid':
                        # Hybrid logic: Signal AND indicators must match
                        # We reuse the logic from Indicator mode
                        if is_long and dataframe['enter_long'].iloc[-1] == 1:
                            dataframe.loc[dataframe.index[-1], 'enter_long'] = 1
                            # Attach signal metadata to the dataframe for confirm_trade_entry
                            dataframe.loc[dataframe.index[-1], 'enter_tag'] = f"hybrid_{sig['idempotency_key']}"
                        elif is_short and dataframe['enter_short'].iloc[-1] == 1:
                            dataframe.loc[dataframe.index[-1], 'enter_short'] = 1
                            dataframe.loc[dataframe.index[-1], 'enter_tag'] = f"hybrid_{sig['idempotency_key']}"
                        else:
                            # Indicator doesn't confirm the signal
                            pass
                    else:
                        # Fallback (should not happen if mode is correctly managed)
                        pass

        return dataframe

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                            time_in_force: str, current_time: datetime, entry_tag: str | None,
                            side: str, **kwargs) -> bool:
        """
        Called right before entering a trade.
        We use this to mark hybrid signals as 'sent'.
        """
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
