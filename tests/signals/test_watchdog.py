import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone
from freqtrade.persistence import Trade
from freqtrade.persistence.models import _CustomData
from freqtrade.enums import ExitType

def test_signal_only_strategy_custom_stoploss():
    """Проверка, что custom_stoploss возвращает правильный процент относительно signal_sl"""
    from user_data.strategies.signal_only_strategy import SignalOnlyStrategy
    strategy = SignalOnlyStrategy({})
    
    trade = MagicMock(spec=Trade)
    trade.is_short = False
    
    # Мокаем custom_data: signal_sl = 0.140
    def mock_get_custom_data(key):
        if key == "signal_sl":
            return "0.140"
        return None
    trade.get_custom_data.side_effect = mock_get_custom_data
    
    # Текущая цена 0.150
    sl_pct = strategy.custom_stoploss(
        pair="DOGE/USDT",
        trade=trade,
        current_time=datetime.now(timezone.utc),
        current_rate=0.150,
        current_profit=0.0
    )
    
    # Ожидаем (0.140 / 0.150) - 1 = -0.066666...
    assert abs(sl_pct - (-0.06666666666666665)) < 0.0001
    
    # Текущая цена упала до 0.145
    sl_pct_2 = strategy.custom_stoploss(
        pair="DOGE/USDT",
        trade=trade,
        current_time=datetime.now(timezone.utc),
        current_rate=0.145,
        current_profit=-0.033
    )
    # Ожидаем (0.140 / 0.145) - 1 = -0.03448...
    assert abs(sl_pct_2 - (-0.03448275862068961)) < 0.0001

def test_signal_only_strategy_custom_exit():
    """Проверка, что custom_exit возвращает сигнал на выход при достижении TP"""
    from user_data.strategies.signal_only_strategy import SignalOnlyStrategy
    strategy = SignalOnlyStrategy({})
    
    trade = MagicMock(spec=Trade)
    trade.is_short = False
    
    def mock_get_custom_data(key):
        if key == "signal_tp":
            return "0.180"
        return None
    trade.get_custom_data.side_effect = mock_get_custom_data
    
    # Цена еще не достигла TP
    exit_signal = strategy.custom_exit(
        pair="DOGE/USDT",
        trade=trade,
        current_time=datetime.now(timezone.utc),
        current_rate=0.170,
        current_profit=0.1
    )
    assert exit_signal is None
    
    # Цена достигла TP
    exit_signal_hit = strategy.custom_exit(
        pair="DOGE/USDT",
        trade=trade,
        current_time=datetime.now(timezone.utc),
        current_rate=0.181,
        current_profit=0.2
    )
    assert exit_signal_hit == "signal_tp_hit"
