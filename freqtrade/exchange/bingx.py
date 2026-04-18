"""Bingx exchange subclass"""

import logging

from freqtrade.enums import MarginMode, TradingMode
from freqtrade.exchange import Exchange
from freqtrade.exchange.exchange_types import FtHas, LeverageTier


logger = logging.getLogger(__name__)

# CCXT BingX leaves ``maxLeverage`` empty in leverage tiers; cap avoids absurd 1/mmr values.
_BINGX_MAX_LEV_APPROX_CAP = 150.0


class Bingx(Exchange):
    """
    BingX: spot and USDT-M linear swap (CCXT ``defaultType: swap``).

    Fork: USDT-M swap aligns with ``scripts/bingx_swap_smoke_trade.py``. Coin-M / «standard»
    futures BingX are out of scope for this class.
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

    def parse_leverage_tier(self, tier: dict) -> LeverageTier:
        """
        CCXT unified tiers for BingX swap set ``maxLeverage`` to ``None``; Freqtrade needs a
        numeric cap per tier for ``get_max_leverage`` / notional limits. Approximate from
        ``maintenanceMarginRate`` when missing (see CCXT ``parse_market_leverage_tiers``).
        """
        info = tier.get("info") or {}
        max_lev = tier.get("maxLeverage")
        mmr = tier.get("maintenanceMarginRate")
        if max_lev is None and mmr is not None:
            try:
                mmr_f = float(mmr)
                if mmr_f > 0:
                    approx = 1.0 / mmr_f
                    max_lev = min(max(approx, 1.0), _BINGX_MAX_LEV_APPROX_CAP)
            except (TypeError, ValueError):
                max_lev = None
        if max_lev is None:
            max_lev = 125.0

        maint_amt = None
        if "cum" in info:
            try:
                maint_amt = float(info["cum"])
            except (TypeError, ValueError):
                maint_amt = None
        elif "maintAmount" in info:
            try:
                maint_amt = float(info["maintAmount"])
            except (TypeError, ValueError):
                maint_amt = None

        return {
            "minNotional": float(tier["minNotional"]),
            "maxNotional": tier.get("maxNotional"),
            "maintenanceMarginRate": float(mmr) if mmr is not None else 0.0,
            "maxLeverage": float(max_lev),
            "maintAmt": maint_amt,
        }
