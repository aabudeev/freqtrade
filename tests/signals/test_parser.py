import pytest
from freqtrade.signals.parser import (
    parse_signal_text,
    SignalType,
    SignalSide,
    SignalEvent
)

def test_parse_valid_long_entry_range():
    text = """LONG
Монета: DOGE
Вход: 0.150 - 0.155
Цель: 0.180
Стоп: 0.140
Плечо: 10x"""
    
    event = parse_signal_text(text)
    assert event is not None
    assert event.type == SignalType.ENTRY
    assert event.symbol == "DOGE/USDT:USDT"
    assert event.side == SignalSide.LONG
    assert event.entry_range == (0.150, 0.155)
    assert event.target == 0.180
    assert event.stop == 0.140
    assert event.leverage == 10

def test_parse_valid_short_entry_single_price():
    text = """SHORT
Монета: BTC
Вход: 65000
Цель: 60000
Стоп: 67000"""
    
    event = parse_signal_text(text)
    assert event is not None
    assert event.type == SignalType.ENTRY
    assert event.symbol == "BTC/USDT:USDT"
    assert event.side == SignalSide.SHORT
    assert event.entry_range == (65000.0, 65000.0)
    assert event.target == 60000.0
    assert event.stop == 67000.0
    assert event.leverage is None

def test_parse_valid_exit_take():
    text = "AXS - тейк ✅"
    event = parse_signal_text(text)
    assert event is not None
    assert event.type == SignalType.TAKE_PROFIT
    assert event.symbol == "AXS/USDT:USDT"
    
    text2 = "DOGE - тейк"
    event2 = parse_signal_text(text2)
    assert event2 is not None
    assert event2.type == SignalType.TAKE_PROFIT
    assert event2.symbol == "DOGE/USDT:USDT"

def test_parse_valid_exit_stop():
    text = "SUI - стоп"
    event = parse_signal_text(text)
    assert event is not None
    assert event.type == SignalType.STOP_LOSS
    assert event.symbol == "SUI/USDT:USDT"

def test_parse_invalid_text():
    assert parse_signal_text("") is None
    assert parse_signal_text("Привет, это не сигнал") is None
    
    # Missing required fields
    incomplete = """LONG
Монета: ETH
Вход: 3000"""
    assert parse_signal_text(incomplete) is None
