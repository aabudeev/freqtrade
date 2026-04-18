"""Tests for BingX exchange (fork: spot + USDT-M swap)."""

from unittest.mock import MagicMock, PropertyMock

import pytest

from freqtrade.enums import MarginMode, TradingMode
from freqtrade.exceptions import ConfigurationError
from freqtrade.exchange import bingx
from tests.conftest import get_patched_exchange


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


def _bingx_futures_stub():
    ex = _bingx_stub()
    ex.trading_mode = TradingMode.FUTURES
    return ex


def test_bingx_futures_pair_validation_rejects_spot_style_whitelist():
    ex = _bingx_futures_stub()
    cfg = {
        "stake_currency": "USDT",
        "exchange": {"pair_whitelist": ["BTC/USDT"], "pair_blacklist": []},
    }
    with pytest.raises(ConfigurationError, match="BASE/QUOTE:QUOTE"):
        ex._validate_bingx_futures_pair_symbols(cfg)


def test_bingx_futures_pair_validation_rejects_mismatched_settle_and_stake():
    ex = _bingx_futures_stub()
    cfg = {
        "stake_currency": "USDT",
        "exchange": {"pair_whitelist": ["ETH/USDC:USDC"], "pair_blacklist": []},
    }
    with pytest.raises(ConfigurationError, match="stake_currency"):
        ex._validate_bingx_futures_pair_symbols(cfg)


def test_bingx_futures_pair_validation_rejects_quote_ne_settle():
    ex = _bingx_futures_stub()
    cfg = {
        "stake_currency": "USDT",
        "exchange": {"pair_whitelist": ["BTC/USD:USDT"], "pair_blacklist": []},
    }
    with pytest.raises(ConfigurationError, match="quote and settle"):
        ex._validate_bingx_futures_pair_symbols(cfg)


def test_bingx_futures_pair_validation_checks_blacklist():
    ex = _bingx_futures_stub()
    cfg = {
        "stake_currency": "USDT",
        "exchange": {"pair_whitelist": ["DOGE/USDT:USDT"], "pair_blacklist": ["BAD/USDT"]},
    }
    with pytest.raises(ConfigurationError, match="BASE/QUOTE:QUOTE"):
        ex._validate_bingx_futures_pair_symbols(cfg)


def test_bingx_futures_pair_validation_accepts_swap_symbols():
    ex = _bingx_futures_stub()
    cfg = {
        "stake_currency": "USDT",
        "exchange": {
            "pair_whitelist": ["DOGE/USDT:USDT", "ETH/USDT:USDT"],
            "pair_blacklist": ["SCAM/USDT:USDT"],
        },
    }
    ex._validate_bingx_futures_pair_symbols(cfg)


def test_bingx_spot_mode_skips_futures_pair_validation():
    ex = _bingx_stub()
    ex.trading_mode = TradingMode.SPOT
    cfg = {
        "stake_currency": "USDT",
        "exchange": {"pair_whitelist": ["BTC/USDT"], "pair_blacklist": []},
    }
    ex._validate_bingx_futures_pair_symbols(cfg)


@pytest.mark.usefixtures("init_persistence")
def test_bingx_get_params_adds_hedged_when_hedge_mode(mocker, default_conf_usdt):
    default_conf_usdt["trading_mode"] = "futures"
    default_conf_usdt["margin_mode"] = "isolated"
    default_conf_usdt["exchange"]["name"] = "bingx"
    default_conf_usdt["exchange"]["pair_whitelist"] = ["ETH/USDT:USDT"]
    default_conf_usdt["exchange"]["pair_blacklist"] = ["DOGE/USDT:USDT"]
    api_mock = MagicMock()
    type(api_mock).has = PropertyMock(
        return_value={"setLeverage": True, "fetchPositionMode": True}
    )
    ex = get_patched_exchange(mocker, default_conf_usdt, api_mock, exchange="bingx")
    ex._bingx_current_hedged = True
    params = ex._get_params("buy", "limit", 5.0, False)
    assert params.get("hedged") is True


@pytest.mark.usefixtures("init_persistence")
def test_bingx_set_leverage_one_way_uses_both(mocker, default_conf_usdt):
    default_conf_usdt["dry_run"] = False
    default_conf_usdt["trading_mode"] = "futures"
    default_conf_usdt["margin_mode"] = "isolated"
    default_conf_usdt["exchange"]["name"] = "bingx"
    default_conf_usdt["exchange"]["pair_whitelist"] = ["ETH/USDT:USDT"]
    default_conf_usdt["exchange"]["pair_blacklist"] = ["DOGE/USDT:USDT"]
    api_mock = MagicMock()
    api_mock.set_leverage = MagicMock(return_value={"ok": True})
    api_mock.fetch_position_mode = MagicMock(return_value={"hedged": False})
    type(api_mock).has = PropertyMock(
        return_value={"setLeverage": True, "fetchPositionMode": True}
    )
    ex = get_patched_exchange(mocker, default_conf_usdt, api_mock, exchange="bingx")
    ex._set_leverage(10, "DOGE/USDT:USDT", accept_fail=False)
    api_mock.set_leverage.assert_called_once_with(10, "DOGE/USDT:USDT", {"side": "BOTH"})


@pytest.mark.usefixtures("init_persistence")
def test_bingx_set_leverage_hedge_sets_long_and_short(mocker, default_conf_usdt):
    default_conf_usdt["dry_run"] = False
    default_conf_usdt["trading_mode"] = "futures"
    default_conf_usdt["margin_mode"] = "isolated"
    default_conf_usdt["exchange"]["name"] = "bingx"
    default_conf_usdt["exchange"]["pair_whitelist"] = ["ETH/USDT:USDT"]
    default_conf_usdt["exchange"]["pair_blacklist"] = ["DOGE/USDT:USDT"]
    api_mock = MagicMock()
    api_mock.set_leverage = MagicMock(return_value={"ok": True})
    api_mock.fetch_position_mode = MagicMock(return_value={"hedged": True})
    type(api_mock).has = PropertyMock(
        return_value={"setLeverage": True, "fetchPositionMode": True}
    )
    ex = get_patched_exchange(mocker, default_conf_usdt, api_mock, exchange="bingx")
    ex._set_leverage(7, "DOGE/USDT:USDT", accept_fail=False)
    assert api_mock.set_leverage.call_count == 2
    api_mock.set_leverage.assert_any_call(7, "DOGE/USDT:USDT", {"side": "LONG"})
    api_mock.set_leverage.assert_any_call(7, "DOGE/USDT:USDT", {"side": "SHORT"})
