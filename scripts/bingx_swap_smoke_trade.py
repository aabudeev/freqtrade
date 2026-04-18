#!/usr/bin/env python3
"""
Тестовая сделка на BingX USDT-M perpetual (swap) через CCXT.

Демо (виртуальный счёт VST), как на странице perpetual в браузере:
  В CCXT: exchange.set_sandbox_mode(True) — запросы идут на
  https://open-api-vst.bingx.com (не на open-api.bingx.com). Это отдельный
  контур от живого счёта; баланс в VST, не реальные USDT
  (см. https://bingx.com/en/support/articles/15510995361817 ).
  Обычно используются те же API key/secret из личного кабинета BingX; если
  ключ отклоняется — смотрите права ключа и раздел API в справке BingX.

ВАЖНО — стандартные фьючерсы (отдельный продукт «Станд. фьючерсы»):
  В CCXT для BingX у API contract/v1 есть только чтение (balance, positions, orders).
  Размещения ордеров на стандартном счёте в unified-методах нет — поэтому сценарий
  «лонг/шорт на VST стандарта» через этот же стек сейчас НЕ поддерживается.
  Флаг --demo относится к USDT-M perpetual demo (swap), не к «стандартным» фьючерсам.

Скрипт (swap):
  1) Плечо 30x (one-way: side=BOTH; hedge: LONG + SHORT)
  2) Лонг: рыночный вход на ~quote_usdt USDT ноционала, затем закрытие reduceOnly
  3) Шорт: рыночный вход sell, затем закрытие buy reduceOnly

  export BINGX_API_KEY=...
  export BINGX_SECRET=...
  python scripts/bingx_swap_smoke_trade.py --demo --execute   # только VST, без реальных USDT
  python scripts/bingx_swap_smoke_trade.py --execute          # реальный USDT-M

Без --execute только план (dry-run). Ключи с правом торговли на фьючерсах.

По умолчанию пара DOGE/USDT:USDT — у BTC часто min объём > 1 USDT ноционала.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

try:
    import ccxt  # type: ignore
except ImportError:
    print("Нужен ccxt: pip install ccxt", file=sys.stderr)
    sys.exit(1)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def set_swap_leverage(ex: "ccxt.Exchange", leverage: int, symbol: str) -> bool:
    """
    BingX: в one-way допустим side=BOTH; в Hedge — только LONG/SHORT/ALL (не BOTH).
    CCXT принимает BOTH | LONG | SHORT — для hedge выставляем плечо на обе стороны.

    Для ордеров в Hedge в create_order нужен params['hedged']=True (иначе BOTH и ошибка API).
    """
    mode = ex.fetch_position_mode(symbol)
    hedged = bool(mode.get("hedged"))
    print(f"--- set_leverage (position mode: {'hedge' if hedged else 'one-way'}) ---")
    if hedged:
        print(ex.set_leverage(leverage, symbol, {"side": "LONG"}))
        print(ex.set_leverage(leverage, symbol, {"side": "SHORT"}))
    else:
        print(ex.set_leverage(leverage, symbol, {"side": "BOTH"}))
    return hedged


def swap_order_params(hedged: bool, **extra: object) -> dict:
    """BingX swap: hedge → передать hedged=True в CCXT (PositionSide LONG/SHORT)."""
    p = dict(extra)
    if hedged:
        p["hedged"] = True
    return p


def main() -> None:
    p = argparse.ArgumentParser(description="BingX USDT-M swap smoke: long+close, short+close")
    p.add_argument(
        "--symbol",
        default="DOGE/USDT:USDT",
        help="Линейный perpetual CCXT (default: DOGE — малый мин. лот vs BTC)",
    )
    p.add_argument(
        "--leverage",
        type=int,
        default=30,
        help="Плечо (default: 30)",
    )
    p.add_argument(
        "--quote-usdt",
        type=float,
        default=10.0,
        help="Целевой ноционал в USDT для входа (default: 10)",
    )
    p.add_argument(
        "--demo",
        action="store_true",
        help="Демо perpetual (VST): CCXT sandbox → open-api-vst.bingx.com, без реальных средств",
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="Реально выставить ордера (без флага — только расчёт и проверки)",
    )
    args = p.parse_args()

    key = os.environ.get("BINGX_API_KEY") or os.environ.get("FREQTRADE__EXCHANGE__KEY")
    secret = os.environ.get("BINGX_SECRET") or os.environ.get("FREQTRADE__EXCHANGE__SECRET")
    if not key or not secret:
        print("Задайте BINGX_API_KEY и BINGX_SECRET", file=sys.stderr)
        sys.exit(1)

    ex = ccxt.bingx(
        {
            "apiKey": key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        }
    )
    if args.demo:
        ex.set_sandbox_mode(True)

    print("=== BingX USDT-M swap smoke ===")
    mode = "DEMO (VST, open-api-vst)" if args.demo else "LIVE (реальный USDT-M)"
    print(
        f"ccxt {ccxt.__version__} | {mode} | symbol={args.symbol} "
        f"leverage={args.leverage}x quote≈{args.quote_usdt} USDT"
    )
    print()

    ex.load_markets()
    if args.symbol not in ex.markets:
        print(f"Неизвестный символ {args.symbol!r}", file=sys.stderr)
        sys.exit(1)

    market = ex.market(args.symbol)
    ticker = ex.fetch_ticker(args.symbol)
    last = float(ticker["last"])
    raw_amount = args.quote_usdt / last
    amount = float(ex.amount_to_precision(args.symbol, raw_amount))

    amin = market.get("limits", {}).get("amount", {}).get("min")
    if amin is not None and amount < float(amin):
        print(
            f"Расчётное количество {amount} < min {amin}. "
            f"Увеличьте --quote-usdt или смените --symbol (например DOGE/USDT:USDT).",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Цена ~{last} USDT, объём базы ~{amount} (ноционал ~{amount * last:.4f} USDT)")
    print()

    if not args.execute:
        print("Dry-run. Для реальной торговли добавьте --execute")
        if args.demo:
            print("Режим --demo: ордера пойдут в VST на open-api-vst (проверьте баланс VST в приложении).")
        else:
            print("Убедитесь, что USDT переведён на счёт USDT-M perpetual (не стандартные фьючерсы).")
        return

    hedged = set_swap_leverage(ex, args.leverage, args.symbol)
    time.sleep(0.5)

    print("--- LONG: market buy ---")
    o1 = ex.create_order(args.symbol, "market", "buy", amount, None, swap_order_params(hedged))
    print(o1)
    time.sleep(2)

    print("--- LONG: market sell reduceOnly ---")
    o2 = ex.create_order(
        args.symbol,
        "market",
        "sell",
        amount,
        None,
        swap_order_params(hedged, reduceOnly=True),
    )
    print(o2)
    time.sleep(2)

    print("--- SHORT: market sell (open) ---")
    o3 = ex.create_order(args.symbol, "market", "sell", amount, None, swap_order_params(hedged))
    print(o3)
    time.sleep(2)

    print("--- SHORT: market buy reduceOnly ---")
    o4 = ex.create_order(
        args.symbol,
        "market",
        "buy",
        amount,
        None,
        swap_order_params(hedged, reduceOnly=True),
    )
    print(o4)

    print()
    print("Готово. Проверьте позиции и баланс в приложении / fetch_positions.")


if __name__ == "__main__":
    main()
