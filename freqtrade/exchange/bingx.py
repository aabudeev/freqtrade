"""Bingx exchange subclass"""

import asyncio
import logging
from math import floor
from typing import Any, Dict

import ccxt

from freqtrade.misc import chunks

from freqtrade.constants import BuySell, Config
from freqtrade.enums import MarginMode, TradingMode
from freqtrade.exceptions import (
    ConfigurationError,
    DDosProtection,
    OperationalException,
    TemporaryError,
)
from freqtrade.exchange import Exchange
from freqtrade.exchange.common import retrier
from freqtrade.exchange.exchange_types import CcxtOrder, FtHas, LeverageTier, CcxtBalances


logger = logging.getLogger(__name__)

# CCXT BingX leaves ``maxLeverage`` empty in leverage tiers; cap avoids absurd 1/mmr values.
_BINGX_MAX_LEV_APPROX_CAP = 150.0
# BingX: ~30 tier API calls / 120s. Use sequential fetches + pause (parallel bursts still burn the quota).
_BINGX_LEVERAGE_TIERS_CHUNK = 1
_BINGX_LEVERAGE_TIERS_SLEEP_S = 4.5


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

    def validate_config(self, config: Config) -> None:
        super().validate_config(config)
        self._validate_bingx_futures_pair_symbols(config)

    def _validate_bingx_futures_pair_symbols(self, config: Config) -> None:
        """
        USDT-M swap uses CCXT unified symbols ``BASE/QUOTE:QUOTE``; spot-style pairs fail at runtime.

        Only ``pair_whitelist`` is checked: ``pair_blacklist`` entries are often regex/wildcards
        (e.g. ``BNB/.*``) and are expanded against markets later, not validated as literal symbols.
        """
        if self.trading_mode != TradingMode.FUTURES:
            return
        stake = (config.get("stake_currency") or "").strip()
        exchange = config.get("exchange") or {}
        for pair in exchange.get("pair_whitelist", []):
            self._check_bingx_swap_pair_symbol(pair, stake, "pair_whitelist")

    def _check_bingx_swap_pair_symbol(self, pair: str, stake: str, list_name: str) -> None:
        if ":" not in pair:
            raise ConfigurationError(
                f"BingX USDT-M swap requires futures pair format BASE/QUOTE:QUOTE "
                f"(e.g. BTC/USDT:USDT), not spot-style symbols. "
                f"Offending entry in exchange.{list_name}: {pair}"
            )
        try:
            base_quote, settle = pair.rsplit(":", 1)
            _base, quote = base_quote.split("/", 1)
        except ValueError as e:
            raise ConfigurationError(
                f"Invalid pair symbol for BingX futures: {pair!r} "
                f"(expected BASE/QUOTE:QUOTE). Found in exchange.{list_name}."
            ) from e
        if quote != settle:
            raise ConfigurationError(
                f"BingX linear swap expects quote and settle currency to match "
                f"(e.g. ETH/USDT:USDT), got {pair!r} in exchange.{list_name}."
            )
        if stake and settle != stake:
            raise ConfigurationError(
                f"BingX futures pair {pair!r} settles in {settle}, but stake_currency is {stake}. "
                f"Use pairs ending with :{stake} in exchange.{list_name}."
            )

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

    def additional_exchange_init(self) -> None:
        self._api.verbose = False
        self._api_async.verbose = False
        super().additional_exchange_init()

        if self._config.get("exchange", {}).get("sandbox"):
            self._api.set_sandbox_mode(True)
            self._api_async.set_sandbox_mode(True)
            if self._ws_async:
                self._ws_async.set_sandbox_mode(True)
            logger.info("BingX Sandbox mode enabled for VST trading")

    def get_balances(self, params: dict | None = None) -> CcxtBalances:
        balances = super().get_balances(params)
        is_vst = getattr(self._api, 'sandbox', False)
        
        # ОТЛАДКА: выводим в консоль что реально пришло
        api_url = self._api.urls.get('api', {})
        active_url = api_url.get('swap', 'unknown') if isinstance(api_url, dict) else api_url
        keys = list(balances.keys())[:10] # первые 10 ключей
        logger.info(f"BALANCES DEBUG (is_vst={is_vst}, URL={active_url}): keys found: {keys}")
        if "USDT" in balances:
            logger.info(f"USDT Balance: {balances['USDT']['total']}")
        if "VST" in balances:
            logger.info(f"VST Balance: {balances['VST']['total']}")

        if is_vst and "VST" in balances:
            balances["USDT"] = balances.pop("VST")
        return balances

    def _bingx_refresh_hedge_flag(self, pair: str) -> None:
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
            try:
                self._api.set_margin_mode('ISOLATED', pair)
            except Exception:
                pass

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

    def fetch_l2_order_book(self, pair: str, limit: int = 100) -> dict:
        valid_limits = [5, 10, 20, 50, 100, 500, 1000]
        bingx_limit = 5
        for v in valid_limits:
            if v >= limit:
                bingx_limit = v
                break
        if limit > 1000:
            bingx_limit = 1000
            
        return super().fetch_l2_order_book(pair, bingx_limit)

    def load_leverage_tiers(self) -> dict[str, list[dict]]:
        if self.trading_mode != TradingMode.FUTURES:
            return {}
        if not self.exchange_has("fetchMarketLeverageTiers"):
            return {}

        markets = self.markets
        symbols = sorted(
            symbol
            for symbol, market in markets.items()
            if (
                self.market_is_future(market)
                and market["quote"] == self._config["stake_currency"]
            )
        )
        total_swaps = len(symbols)
        pair_whitelist = self._config.get("exchange", {}).get("pair_whitelist") or []
        wl_set = set(pair_whitelist)
        if wl_set:
            symbols = [s for s in symbols if s in wl_set]
            logger.info(
                "BingX: load_leverage_tiers (fork): %s whitelist pair(s) for tiers "
                "(%s config entries, %s USDT swaps total on exchange).",
                len(symbols),
                len(pair_whitelist),
                total_swaps,
            )
            if not symbols:
                logger.warning(
                    "BingX: pair_whitelist matches no USDT-M swap markets for leverage tiers. "
                    "Use symbols like BASE/USDT:USDT."
                )
                return {}
        else:
            logger.warning(
                "BingX: load_leverage_tiers (fork): pair_whitelist empty — fetching tiers for "
                "all %s swaps (very slow, 109429 risk). Set exchange.pair_whitelist.",
                total_swaps,
            )

        tiers: dict[str, list[dict]] = {}
        tiers_cached = self.load_cached_leverage_tiers(self._config["stake_currency"])
        if tiers_cached:
            symset = set(symbols)
            tiers = {k: v for k, v in tiers_cached.items() if k in symset}

        coros = [self.get_market_leverage_tiers(symbol) for symbol in symbols if symbol not in tiers]

        if coros:
            logger.info(
                "BingX: fetching leverage tiers for %s symbol(s) (chunk=%s, inter-chunk sleep=%ss).",
                len(coros),
                _BINGX_LEVERAGE_TIERS_CHUNK,
                _BINGX_LEVERAGE_TIERS_SLEEP_S,
            )
        else:
            logger.info("Using cached leverage_tiers.")

        async def gather_results(input_coro):
            return await asyncio.gather(*input_coro, return_exceptions=True)

        async def chunk_sleep():
            await asyncio.sleep(_BINGX_LEVERAGE_TIERS_SLEEP_S)

        chunk_list = list(chunks(coros, _BINGX_LEVERAGE_TIERS_CHUNK))
        for i, input_coro in enumerate(chunk_list):
            with self._loop_lock:
                results = self.loop.run_until_complete(gather_results(input_coro))

            for res in results:
                if isinstance(res, Exception):
                    logger.warning("Leverage tier exception: %s", repr(res))
                    continue
                symbol, tier = res
                tiers[symbol] = tier

            if i < len(chunk_list) - 1:
                with self._loop_lock:
                    self.loop.run_until_complete(chunk_sleep())

        if coros:
            self.cache_leverage_tiers(tiers, self._config["stake_currency"])
        logger.info("BingX: leverage tiers ready for %s market(s).", len(symbols))

        return tiers

    def parse_leverage_tier(self, tier: dict) -> LeverageTier:
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
