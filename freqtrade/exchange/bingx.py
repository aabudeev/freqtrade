"""Bingx exchange subclass"""

import logging

from freqtrade.enums import MarginMode, TradingMode
from freqtrade.exchange import Exchange
from freqtrade.exchange.exchange_types import FtHas


logger = logging.getLogger(__name__)


class Bingx(Exchange):
    """
    BingX: spot and USDT-M linear swap (CCXT ``defaultType: swap``).

    Fork: futures path targets the same swap product as ``scripts/bingx_swap_smoke_trade.py``.
    BingX «standard» futures remain out of scope (see BINGX_FUTURES_GAP_ANALYSIS.md).
    """

    _ft_has: FtHas = {
        "ohlcv_candle_limit": 1000,
        "stoploss_on_exchange": True,
        "stoploss_order_types": {"limit": "limit", "market": "market"},
        "order_time_in_force": ["GTC", "IOC", "PO"],
        "trades_has_history": False,  # Endpoint doesn't seem to support pagination
    }

    _ft_has_futures: FtHas = {
        # Funding history OHLCV cap (used when fetching funding rates)
        "funding_fee_candle_limit": 200,
        "has_delisting": True,
        # Spot BingX sets stoploss_on_exchange; futures keeps non-blocking stops where applicable
        "stoploss_blocks_assets": False,
    }

    _supported_trading_mode_margin_pairs: list[tuple[TradingMode, MarginMode]] = [
        (TradingMode.SPOT, MarginMode.NONE),
        (TradingMode.FUTURES, MarginMode.ISOLATED),
        # Enable when cross-margin swap is verified end-to-end on BingX:
        # (TradingMode.FUTURES, MarginMode.CROSS),
    ]
