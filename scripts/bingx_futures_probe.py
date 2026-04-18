#!/usr/bin/env python3
"""
Проверка BingX через CCXT.

Режимы (см. BingX: USDT-M перпы, Coin-M, стандартные фьючерсы):
  --mode standard  — стандартные фьючерсы (stdFutures): баланс/позиции через params.standard
  --mode swap        — бессрочный USDT-M (swap), как в Freqtrade / прошлая версия скрипта

  export BINGX_API_KEY="..."
  export BINGX_SECRET="..."
  python scripts/bingx_futures_probe.py
  python scripts/bingx_futures_probe.py --mode swap --symbol ETH/USDT:USDT

Ключи не храните в репозитории. Опционально: pip install python-dotenv и .env рядом со скриптом.

Если .venv создавали через sudo: sudo chown -R "$USER:$USER" .venv

Без venv: скопируйте в user_data/tools/ и
  docker compose exec freqtrade python /freqtrade/user_data/tools/bingx_futures_probe.py

Только чтение, без ордеров.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

try:
    import ccxt  # type: ignore
except ImportError:
    print("Нужен пакет ccxt: pip install ccxt", file=sys.stderr)
    sys.exit(1)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _short(obj: Any, limit: int = 1200) -> str:
    s = json.dumps(obj, indent=2, default=str)
    if len(s) > limit:
        return s[:limit] + f"\n... ({len(s) - limit} символов обрезано)"
    return s


def _make_ex(key: str, secret: str, default_type: str) -> Any:
    return ccxt.bingx(
        {
            "apiKey": key or "",
            "secret": secret or "",
            "enableRateLimit": True,
            "options": {
                "defaultType": default_type,
            },
        }
    )


def run_standard(key: str, secret: str, args: argparse.Namespace) -> None:
    """Стандартные фьючерсы: CCXT использует contract API при params['standard']=True."""
    # defaultType не влияет на ветку standard в fetch_balance — оставляем spot, чтобы load_markets был легче
    ex = _make_ex(key, secret, "spot")

    print("=== BingX — стандартные фьючерсы (stdFutures) ===")
    print(f"Версия ccxt: {ccxt.__version__}")
    print(
        "CCXT: fetch_balance/positions с params standard=True → "
        "contractV1 (см. исходники ccxt bingx.py)."
    )
    print()

    print("--- load_markets (справочно: в unified часто нет отдельного списка std futures) ---")
    try:
        markets = ex.load_markets()
        print(f"Всего рынков в unified: {len(markets)}")
    except Exception as e:
        print(f"Ошибка load_markets: {e}")
    print()

    print(
        "--- Справочно: тикер USDT-M перпа (не стандартный контракт, только цена BTC) ---"
    )
    ex_swap = _make_ex(key, secret, "swap")
    try:
        t = ex_swap.fetch_ticker(args.symbol)
        print(_short({k: t[k] for k in ("symbol", "last", "bid", "ask", "timestamp") if k in t}))
    except Exception as e:
        print(f"Ошибка: {e}")
    print()

    if args.skip_private or not key or not secret:
        print("--- приватные (standard) пропущены ---")
        return

    ex.apiKey = key
    ex.secret = secret

    print("--- fetch_balance(params.standard=True) — баланс стандартных фьючерсов ---")
    try:
        bal = ex.fetch_balance({"standard": True})
        interesting: dict[str, Any] = {}
        for k in ("USDT", "USDC", "VST", "free", "used", "total", "info"):
            if k in bal:
                interesting[k] = bal[k]
        print(_short(interesting if interesting else bal, 2500))
    except Exception as e:
        print(f"Ошибка: {e}")
    print()

    print("--- fetch_positions(None, params.standard=True) — позиции стандартных фьючерсов ---")
    try:
        pos = ex.fetch_positions(None, {"standard": True})
        print(_short(pos, 3500))
    except Exception as e:
        print(f"Ошибка: {e}")
    print()

    print(
        "Готово (standard). Открытые ордера std в CCXT смотрите через "
        "fetch_closed_orders(..., {'standard': True}) или документацию BingX."
    )


def run_swap(key: str, secret: str, args: argparse.Namespace) -> None:
    """USDT-M perpetual (swap) — прежнее поведение."""
    ex = _make_ex(key, secret, "swap")

    print("=== BingX — USDT-M perpetual (swap) ===")
    print(f"Версия ccxt: {ccxt.__version__}")
    print(f"defaultType: {ex.options.get('defaultType')}")
    print()

    print("--- load_markets (swap) ---")
    try:
        markets = ex.load_markets()
        swap = [m for m, mi in markets.items() if mi.get("swap") or mi.get("type") == "swap"]
        print(f"Всего рынков: {len(markets)}, swap/perp в выборке: {len(swap)}")
        if args.symbol in markets:
            print(f"Символ {args.symbol!r} найден, тип: {_short(markets[args.symbol], 800)}")
        else:
            print(f"Символ {args.symbol!r} не в markets. Примеры swap: {swap[:8]}")
    except Exception as e:
        print(f"Ошибка load_markets: {e}")
    print()

    print(f"--- fetch_ticker({args.symbol!r}) ---")
    try:
        t = ex.fetch_ticker(args.symbol)
        print(_short({k: t[k] for k in ("symbol", "last", "bid", "ask", "timestamp") if k in t}))
    except Exception as e:
        print(f"Ошибка: {e}")
    print()

    print(f"--- fetch_ohlcv({args.symbol!r}, 1h, limit=3) ---")
    try:
        ohlcv = ex.fetch_ohlcv(args.symbol, "1h", limit=3)
        print(_short(ohlcv))
    except Exception as e:
        print(f"Ошибка: {e}")
    print()

    print("--- fetch_funding_rate ---")
    try:
        if ex.has.get("fetchFundingRate"):
            fr = ex.fetch_funding_rate(args.symbol)
            print(_short(fr))
        else:
            print("fetchFundingRate: не объявлено в has")
    except Exception as e:
        print(f"Ошибка: {e}")
    print()

    if args.skip_private or not key or not secret:
        print("--- приватные методы пропущены ---")
        return

    ex.apiKey = key
    ex.secret = secret

    print("--- fetch_balance (swap) ---")
    try:
        bal = ex.fetch_balance()
        interesting: dict[str, Any] = {}
        for k in ("USDT", "free", "used", "total", "info"):
            if k in bal:
                interesting[k] = bal[k]
        print(_short(interesting if interesting else bal, 2000))
    except Exception as e:
        print(f"Ошибка: {e}")
    print()

    print("--- fetch_positions ---")
    try:
        if ex.has.get("fetchPositions"):
            pos = ex.fetch_positions([args.symbol])
            print(_short(pos, 2500))
        else:
            print("fetchPositions: не объявлено в has")
    except Exception as e:
        print(f"Ошибка: {e}")
    print()

    print("--- fetch_open_orders ---")
    try:
        if ex.has.get("fetchOpenOrders"):
            oo = ex.fetch_open_orders(args.symbol)
            print(_short(oo, 1500))
        else:
            print("fetchOpenOrders: не объявлено в has")
    except Exception as e:
        print(f"Ошибка: {e}")
    print()

    print("Готово (swap). Права ключа: USDT-M perpetual на BingX.")


def main() -> None:
    p = argparse.ArgumentParser(description="BingX probe: standard futures vs USDT-M swap (CCXT)")
    p.add_argument(
        "--mode",
        choices=("standard", "swap"),
        default="standard",
        help="standard = стандартные фьючерсы BingX; swap = USDT-M перп (по умолчанию: standard)",
    )
    p.add_argument(
        "--symbol",
        default="BTC/USDT:USDT",
        help="Символ USDT-M перпа для справочного тикера (режим standard) или основной символ (режим swap)",
    )
    p.add_argument(
        "--skip-private",
        action="store_true",
        help="Не вызывать приватные методы даже если есть ключи",
    )
    args = p.parse_args()

    key = os.environ.get("BINGX_API_KEY") or os.environ.get("FREQTRADE__EXCHANGE__KEY")
    secret = os.environ.get("BINGX_SECRET") or os.environ.get("FREQTRADE__EXCHANGE__SECRET")

    if args.mode == "standard":
        run_standard(key or "", secret or "", args)
    else:
        run_swap(key or "", secret or "", args)


if __name__ == "__main__":
    main()
