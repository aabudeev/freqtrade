"""Tests for BingX exchange (fork: spot + USDT-M swap)."""

from freqtrade.enums import MarginMode, TradingMode
from freqtrade.exchange import bingx


def test_bingx_supported_trading_mode_margin_pairs():
    pairs = bingx.Bingx._supported_trading_mode_margin_pairs
    assert (TradingMode.SPOT, MarginMode.NONE) in pairs
    assert (TradingMode.FUTURES, MarginMode.ISOLATED) in pairs
    assert (TradingMode.FUTURES, MarginMode.CROSS) not in pairs


def test_bingx_combine_ft_has_futures():
    combined = bingx.Bingx.combine_ft_has(include_futures=True)
    assert combined.get("funding_fee_candle_limit") == 200
    assert combined.get("has_delisting") is True
    assert combined.get("ccxt_futures_name") == "swap"
    assert combined.get("stoploss_blocks_assets") is False
    assert combined.get("stoploss_on_exchange") is True
