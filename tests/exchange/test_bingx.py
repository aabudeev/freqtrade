"""Tests for BingX exchange (fork: spot + USDT-M swap)."""

from freqtrade.enums import MarginMode, TradingMode
from freqtrade.exchange import bingx


def _bingx_stub():
    """Instance shell — ``parse_leverage_tier`` does not use full ``__init__``."""
    ex = bingx.Bingx.__new__(bingx.Bingx)
    # So Exchange.__del__ → close() does not touch missing attributes (pytest GC).
    ex._exchange_ws = None
    ex._api_async = None
    ex._ws_async = None
    ex.loop = None
    return ex


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


def test_bingx_parse_leverage_tier_fills_max_from_maintenance_margin_rate():
    """CCXT BingX tiers omit maxLeverage; derive from maintenanceMarginRate (capped)."""
    ex = _bingx_stub()
    raw = {
        "minNotional": 0.0,
        "maxNotional": 900_000.0,
        "maintenanceMarginRate": 0.01,
        "maxLeverage": None,
        "info": {"maintAmount": "1.5"},
    }
    parsed = ex.parse_leverage_tier(raw)
    assert parsed["maxLeverage"] == 100.0
    assert parsed["maintAmt"] == 1.5
    assert parsed["minNotional"] == 0.0


def test_bingx_parse_leverage_tier_caps_high_leverage_approximation():
    ex = _bingx_stub()
    raw = {
        "minNotional": 0.0,
        "maxNotional": None,
        "maintenanceMarginRate": 0.001,
        "maxLeverage": None,
        "info": {},
    }
    parsed = ex.parse_leverage_tier(raw)
    assert parsed["maxLeverage"] == 150.0


def test_bingx_parse_leverage_tier_respects_explicit_max_leverage():
    ex = _bingx_stub()
    raw = {
        "minNotional": 0.0,
        "maxNotional": 100.0,
        "maintenanceMarginRate": 0.05,
        "maxLeverage": 20.0,
        "info": {},
    }
    parsed = ex.parse_leverage_tier(raw)
    assert parsed["maxLeverage"] == 20.0
