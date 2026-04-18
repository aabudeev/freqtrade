"""Bingx exchange subclass"""

import logging
from math import floor

import ccxt

from freqtrade.constants import BuySell
from freqtrade.enums import MarginMode, TradingMode
from freqtrade.exceptions import DDosProtection, OperationalException, TemporaryError
from freqtrade.exchange import Exchange
from freqtrade.exchange.common import retrier
from freqtrade.exchange.exchange_types import CcxtOrder, FtHas, LeverageTier


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

    def create_order(
        self,
        *,
        pair: str,
        ordertype: str,
        side: BuySell,
        amount: float,
        rate: float,
        leverage: float,
        time_in_force: str = "GTC",
        reduceOnly: bool = False,
        initial_order: bool = True,
    ) -> CcxtOrder:
        """
        Refresh hedge / one-way flag before building params (``_get_params`` runs before
        ``_lev_prep`` in ``create_order``).
        """
        if self.trading_mode == TradingMode.FUTURES and not self._config.get("dry_run", False):
            self._bingx_refresh_hedge_flag(pair)
        return super().create_order(
            pair=pair,
            ordertype=ordertype,
            side=side,
            amount=amount,
            rate=rate,
            leverage=leverage,
            time_in_force=time_in_force,
            reduceOnly=reduceOnly,
            initial_order=initial_order,
        )

    def _bingx_refresh_hedge_flag(self, pair: str) -> None:
        """Cache ``fetch_position_mode`` per pair (BingX swap API is mode-wide; avoid extra calls)."""
        if self.trading_mode != TradingMode.FUTURES:
            self._bingx_current_hedged = False
            return
        if getattr(self, "_bingx_hedge_pair", None) == pair and hasattr(self, "_bingx_current_hedged"):
            return
        self._bingx_hedge_pair = pair
        self._bingx_current_hedged = False
        if self._config.get("dry_run") or not self.exchange_has("fetchPositionMode"):
            return
        try:
            mode = self._api.fetch_position_mode(pair)
            self._bingx_current_hedged = bool(mode.get("hedged"))
        except ccxt.BaseError as e:
            logger.debug("BingX fetch_position_mode for %s: %s", pair, e)

    def _get_params(
        self,
        side: BuySell,
        ordertype: str,
        leverage: float,
        reduceOnly: bool,
        time_in_force: str = "GTC",
    ) -> dict:
        params = super()._get_params(
            side=side,
            ordertype=ordertype,
            leverage=leverage,
            reduceOnly=reduceOnly,
            time_in_force=time_in_force,
        )
        if self.trading_mode == TradingMode.FUTURES and getattr(self, "_bingx_current_hedged", False):
            params["hedged"] = True
        return params

    @retrier
    def _set_leverage(
        self,
        leverage: float,
        pair: str | None = None,
        accept_fail: bool = False,
    ) -> None:
        """
        BingX ``setLeverage`` requires ``params.side``: ``BOTH`` (one-way) or ``LONG``/``SHORT`` (hedge).
        Base implementation omits this and fails on swap markets.
        """
        if self._config["dry_run"] or not self.exchange_has("setLeverage"):
            return
        if self.trading_mode != TradingMode.FUTURES or not pair:
            return super()._set_leverage(leverage, pair, accept_fail)

        if pair != getattr(self, "_bingx_hedge_pair", None):
            self._bingx_refresh_hedge_flag(pair)

        if self._ft_has.get("floor_leverage", False):
            leverage = floor(leverage)
        lev = int(leverage)

        try:
            if getattr(self, "_bingx_current_hedged", False):
                for pos_side in ("LONG", "SHORT"):
                    res = self._api.set_leverage(lev, pair, {"side": pos_side})
                    self._log_exchange_response("set_leverage", res)
            else:
                res = self._api.set_leverage(lev, pair, {"side": "BOTH"})
                self._log_exchange_response("set_leverage", res)
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.BadRequest, ccxt.OperationRejected, ccxt.InsufficientFunds) as e:
            if not accept_fail:
                raise TemporaryError(
                    f"Could not set leverage due to {e.__class__.__name__}. Message: {e}"
                ) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Could not set leverage due to {e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

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
