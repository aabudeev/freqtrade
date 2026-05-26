# Signal Copy Trade Bot — Архитектура и руководство по эксплуатации

> **Последнее обновление**: 2026-05-06
> **Статус**: Стабильно, работает на BingX VST (Sandbox)
> **Стратегия**: `SignalOnlyStrategy`

---

## Содержание

- [Обзор](#обзор)
- [Архитектура системы](#архитектура-системы)
- [Поток сигналов](#поток-сигналов)
- [Жизненный цикл сделки](#жизненный-цикл-сделки)
- [Ключевые компоненты](#ключевые-компоненты)
- [Логика реконсиляции](#логика-реконсиляции)
- [Маппинг сторон ордеров](#маппинг-сторон-ордеров)
- [Известные нюансы и проектные решения](#известные-нюансы-и-проектные-решения)
- [Режимы отказа и защиты](#режимы-отказа-и-защиты)
- [Справочник по конфигурации](#справочник-по-конфигурации)
- [Развёртывание](#развёртывание)
- [Устранение неполадок](#устранение-неполадок)
- [История изменений](#история-изменений)

---

## Обзор

Это **бот для копирования торговых сигналов**, построенный поверх Freqtrade. Он слушает Telegram-канал на предмет торговых сигналов (ENTRY, TAKE_PROFIT, STOP_LOSS), открывает позиции на бирже BingX и автоматически управляет TP/SL ордерами.

Бот **НЕ** использует встроенную генерацию сигналов Freqtrade (индикаторы, анализ dataframe). Вместо этого он полностью опирается на внешние Telegram-сигналы, обрабатываемые кастомным `SignalWorker`.

### Ключевые характеристики
- **Биржа**: BingX (swap/futures, маржа в USDT)
- **Режим**: VST (Виртуальная/Sandbox торговля) — настраивается для реальной
- **Плечо**: Как указано в сигнале (обычно 25x)
- **Ставка на сделку**: 20 USDT
- **Прокси**: Весь трафик идёт через SOCKS5 прокси (`amneziawg2:1080`)
- **База данных**: SQLite (`user_data/trades_signals.sqlite`)

---

## Архитектура системы

```
┌────────────────────────────────────────────────────────────────┐
│                    Docker-контейнер                            │
│                                                                │
│  ┌──────────────────┐    ┌─────────────────────────────────┐   │
│  │ TelegramSignals  │    │         SignalWorker            │   │
│  │ Listener         │───▶│       (фоновый поток)           │   │
│  │ (Telethon)       │    │                                 │   │
│  │                  │    │  • Парсит сигналы               │   │
│  │ Слушает канал    │    │  • Открывает сделки через RPC   │   │
│  │ -1001566432615   │    │  • Ставит TP через CCXT API     │   │
│  └──────────────────┘    │  • Устанавливает SL             │   │
│           │              │  • Диагностика сети             │   │
│           ▼              └──────────┬──────────────────────┘   │
│  ┌───────────────────┐              │                          │
│  │ signals.db        │              │ RPC вызовы               │
│  │ (очередь сигналов)│              ▼                          │
│  └───────────────────┘   ┌─────────────────────────────────┐   │
│                          │      FreqtradeBot               │   │
│                          │  (основной цикл, каждые ~5с)    │   │
│                          │                                 │   │
│                          │  • manage_open_orders()         │   │
│                          │  • exit_positions()             │   │
│                          │  • bot_loop_start() → Reconcile │   │
│                          │  • handle_onexchange_order()    │   │
│                          └──────────┬──────────────────────┘   │
│                                     │                          │
│                          ┌──────────┴─────────────────────┐    │
│                          │  trades_signals.sqlite         │    │
│                          │  (сделки, ордера, custom_data) │    │
│                          └────────────────────────────────┘    │
│                                     │                          │
└─────────────────────────────────────┼──────────────────────────┘
                                      │ CCXT / REST API
                                      ▼
                            ┌──────────────────┐
                            │    Биржа BingX   │
                            │  (через SOCKS5)  │
                            └──────────────────┘
```

---

## Поток сигналов

### 1. Приём сигнала

```
Telegram-канал → TelegramSignalsListener → signals.db (SQLite очередь)
```

- **Listener** работает в отдельном потоке на Telethon (MTProto)
- Сообщения парсятся и сохраняются в `signals.db` со статусом `pending`
- Типы сигналов: `ENTRY`, `TAKE_PROFIT`, `STOP_LOSS`

### 2. Обработка сигнала

```
SignalWorker.process_once() → claim_pending() → парсинг → исполнение
```

- Worker опрашивает `signals.db` каждые несколько секунд
- Забирает pending-сигналы и обрабатывает последовательно
- Пример формата сигнала:
  ```
  📈 LONG
  ▪️Монета: AVAX
  ▪️Плечо: 25-50х
  ▪️Вход: от 9.118 до 9.4
  ▪️Цель: 9.494
  ▪️Стоп: 8.836
  ```

### 3. Типы сигналов и действия

| Тип сигнала | Действие |
|-------------|----------|
| `ENTRY` | Открыть сделку через `_rpc_force_entry()`, установить SL, разместить TP |
| `TAKE_PROFIT` | Закрыть существующую сделку через `_rpc_force_exit()` |
| `STOP_LOSS` | Закрыть существующую сделку через `_rpc_force_exit()` |

---

## Жизненный цикл сделки

### Вход (сигнал ENTRY)

1. `SignalWorker` получает сигнал ENTRY и проводит **pre-flight проверки**:
   - **Spread Check**: Если bid/ask спред > 2%, сделка пропускается (`skipped` навсегда).
   - **Entry Range Check**: Текущая цена сравнивается с диапазоном входа (с буфером ±0.5% на разницу бирж). Если цена далеко, сигнал возвращается в `pending` (до 10 повторных попыток с интервалом 1 минута).
2. `_rpc_force_entry(pair, price=None, order_type="market")` → Freqtrade открывает сделку.
3. Freqtrade создаёт `Trade` + `Order` (ft_order_side=`'buy'` для LONG).
4. Worker устанавливает `trade.stop_loss`, `trade.stoploss`, `trade.stop_loss_pct` (используется авто-SL 2.5% если в сигнале нет).
5. **Emergency Exit Check**: Если после открытия сделки цена *уже* находится за уровнем SL, происходит немедленный `_rpc_force_exit` для предотвращения висящей без защиты позиции.
6. Worker размещает **TP лимитный ордер** на бирже через прямой CCXT API (используется авто-TP 3.5% если в сигнале нет).
7. TP ордер **регистрируется в БД** с `ft_order_side=trade.exit_side` (например `'sell'`).
8. Reconcile (следующий `bot_loop_start`) размещает **SL триггер-ордер** на бирже.

### Срабатывание TP (биржа исполняет TP ордер)

1. `manage_open_orders()` → `fetch_order()` для каждого открытого ордера
2. Если статус TP ордера = `'closed'` → `update_trade_state()` → `trade.update_trade()`
3. `order.ft_order_side == trade.exit_side` → совпадает → сделка закрывается
4. `trade.close(price)` → `is_open=0`, `close_rate`, `close_profit` устанавливаются

### Срабатывание SL (биржа исполняет SL триггер) или Призрак-сделка (Ghost Trade)

1. Позиция исчезает с биржи.
2. `exit_positions()` → `check_exit_amount()` → кошелёк показывает 0 баланс.
3. `handle_onexchange_order()` → получает все ордера с биржи.
4. Находит исполненный sell ордер → создаёт/обновляет запись Order → `update_trade_state()`.
5. Сделка закрывается с `exit_reason='sold_on_exchange'`.
6. **3-tier P&L Recovery (Реконсиляция Призраков)**: Если Freqtrade пропустил момент закрытия позиции (например, бот был выключен), он пытается восстановить точную цену выхода для сохранения статистики:
   - *Tier 1*: Поиск закрытого ордера в истории биржи.
   - *Tier 2*: Ticker (текущая цена), если ордер не найден.
   - *Tier 3*: Open Rate (безубыток) как крайний fallback.

### TP/SL сигнал из канала

1. `SignalWorker` получает сигнал TAKE_PROFIT или STOP_LOSS
2. Находит соответствующую открытую сделку по паре
3. `_rpc_force_exit(trade_id)` → Freqtrade отменяет существующие ордера, размещает рыночный выход
4. Сделка закрывается с `exit_reason='force_exit'`

---

## Ключевые компоненты

### Файлы

| Файл | Назначение |
|------|-----------|
| `user_data/strategies/SignalOnlyStrategy.py` | Класс стратегии, логика реконсиляции, хуки выхода из сделок |
| `freqtrade/signals/worker.py` | SignalWorker: обработка сигналов, размещение TP/SL, диагностика сети |
| `freqtrade/signals/telegram_listener.py` | Listener Telegram-канала на Telethon |
| `freqtrade/signals/signal_store.py` | SQLite очередь для сигналов (`signals.db`) |
| `freqtrade/signals/signal_parser.py` | Парсинг текста Telegram-сообщения в SignalEvent |
| `freqtrade/persistence/trade_model.py` | Trade/Order ORM модели (модифицировано: авто-фикс + fallback ft_price) |
| `freqtrade/freqtradebot.py` | Основной цикл Freqtrade (без изменений кроме поддержки кастомной биржи) |
| `freqtrade/exchange/bingx.py` | Адаптер BingX (тиры плечей, sandbox режим) |

### SignalOnlyStrategy

```python
# Ключевые настройки
minimal_roi = {"0": 10.0}          # Фактически отключено (1000% ROI)
stoploss = -0.99                    # Фактически отключено (99% убытка)
trailing_stop = False
use_custom_stoploss = False
use_exit_signal = False
stoploss_on_exchange = False        # SL управляется worker/reconcile, не ядром FT

unfilledtimeout = {
    'entry': 10,                    # Отмена незаполненных входов через 10 мин
    'exit': 525600,                 # 365 дней — TP ордера НЕ должны авто-отменяться
}
```

### SignalWorker

- Работает в **отдельном потоке** (`threading.Thread`, daemon=True)
- Обрабатывает сигналы из `signals.db`
- Размещает TP ордера через **прямой CCXT API** (`swapV2PrivatePostTradeOrder`)
- Регистрирует TP ордера в БД Freqtrade (модель `Order`)
- Запускает диагностику сети каждые 10 минут
- Валидирует цену SL относительно цены ликвидации

---

## Логика реконсиляции

Реконсиляция запускается в `SignalOnlyStrategy.bot_loop_start()`, с троттлингом каждые 5 минут (`_reconcile_interval = 300`).

### Что делает:

1. **Получает состояние биржи** для каждой открытой сделки:
   - Открытые ордера (CCXT `fetch_open_orders`)
   - Pending/триггер ордера (BingX raw API `swapV2PrivateGetTradePendingOrders`)
   - Позиции (из `fetch_positions`)

2. **Обнаружение орфанов**: Если у сделки нет позиции И нет ордеров на бирже:
   - Логирует warning: `"Trade X appears to have NO position and NO orders"`
   - **Пропускает** размещение TP/SL (`continue`)
   - `handle_onexchange_order()` в основном цикле закроет её

3. **Реконсиляция TP**: Если TP ордер не найден в БД (`ft_order_side == trade.exit_side` и `ft_is_open`):
   - Сканирует ордера на бирже на совпадение (та же сторона, цена в пределах 0.5%)
   - Если найден → регистрирует в БД
   - Если не найден → размещает новый TP лимитный ордер на бирже

4. **Реконсиляция SL**: Если SL ордер не найден в БД (`ft_order_side == 'stoploss'` и `ft_is_open`):
   - Сканирует pending ордера на совпадение (тип STOP_MARKET)
   - Если найден → регистрирует в БД
   - Если не найден → размещает новый SL триггер-ордер на бирже

### Парсинг ответа BingX при размещении TP

BingX возвращает ID TP ордера во вложенной структуре:
```json
{"data": {"order": {"orderId": 2051746376414400512}}}
```

Код обрабатывает оба формата: `data.orderId` и `data.order.orderId`.

---

## Маппинг сторон ордеров

**Критично**: `update_trade()` в Freqtrade валидирует `ft_order_side` по этим значениям:

| Направление сделки | `entry_side` | `exit_side` | Сторона SL |
|-------------------|-------------|------------|-----------|
| LONG | `'buy'` | `'sell'` | `'stoploss'` |
| SHORT | `'sell'` | `'buy'` | `'stoploss'` |

**Никогда не используйте `'exit'` как `ft_order_side`.** Это был legacy-баг, вызывавший краш-циклы `ValueError`.

Если встречается неизвестный `ft_order_side`, `trade_model.py` теперь **авто-исправляет** его:
```python
# Вместо краша:
# raise ValueError(f"Unknown order type: {order.order_type}")

# Теперь авто-фикс:
logger.warning(f"Unknown ft_order_side '{order.ft_order_side}' ... Mapping to exit_side")
order.ft_order_side = self.exit_side
```

---

## Известные нюансы и проектные решения

### 1. `stoploss_on_exchange: False`
- Freqtrade НЕ управляет SL ордерами через свой стандартный механизм
- SL размещается `SignalWorker` (устанавливает `trade.stop_loss`) и `reconcile` (размещает STOP_MARKET на бирже)
- При срабатывании SL, `handle_onexchange_order()` обнаруживает закрытую позицию и закрывает сделку
- Это **by design** — стандартный SL механизм Freqtrade конфликтует с нашими сигнальными ценами SL

### 2. Ограничение SL ценой ликвидации
- Если SL из сигнала за пределами цены ликвидации, worker обрезает его:
  ```
  Signal SL 8.836 is beyond liquidation 9.04184. Capping SL.
  ```
- Формула: `ликвидация ± 3%` буфер

### 3. Предупреждения о несовпадении объёма (BingX swap)
- CCXT для BingX swap задаёт `contractSize=1`, а `fetch_my_trades` для части fills отдаёт `qty` в шагах контракта (`tradeMinQuantity` из `market.info`), не в монетах.
- Симптом: `WARNING: Amount X does not match amount Y` (например SOL `0.12` vs `4.19`, DOGE `70480` vs `3524`).
- **Исправление (fork):** `freqtrade/exchange/bingx.py` — `_trades_contracts_to_amount` масштабирует qty-only fills; `_order_contracts_to_amount` масштабирует exit/TP, если `filled` в шагах контракта, а `cost` уже в USDT (иначе short PnL в UI зеркалится, ~−16% вместо +16%).
- После фикса fee-sync и `close_profit` должны совпадать с биржей; предупреждение не игнорировать.

### 4. Модель потоков
- `SignalWorker` работает в отдельном потоке от основного цикла `FreqtradeBot`
- Оба обращаются к одной SQLAlchemy сессии
- При текущем объёме сигналов (несколько в день) race conditions крайне маловероятны
- При значительном росте объёма рассмотреть `scoped_session`

### 5. Сеть/Прокси
- Весь трафик через SOCKS5 прокси (`amneziawg2:1080` через WireGuard/AmneziaWG)
- Worker запускает диагностику сети каждые 10 минут
- Если прокси падает: API вызовы таймаутятся → worker ретраит → сигналы остаются в `pending`
- Telethon переподключается автоматически при разрыве соединения

### 6. `fetch_order()` RequestTimeout для старых ордеров
- BingX VST API иногда таймаутится при запросе очень старых ID ордеров
- Ретраи (до 5 раз) обычно решают проблему
- Если постоянно: ордер больше не существует на бирже

---

## Режимы отказа и защиты

| Отказ / Угроза | Защита | Результат |
|-------|--------|-----------|
| Высокая волатильность/низкая ликвидность | Проверка спреда (max 2%) перед входом | Сигнал пропускается |
| Цена вне диапазона входа (проскальзывание) | Проверка `entry_range` с буфером ±0.5% | Ретрай до 10 мин, затем `skipped` |
| Сделка открыта, но цена уже пробила SL | `Emergency Exit` сразу после входа | Рыночный выход, предотвращение потери |
| Сигнал без SL или TP | `Auto-SL` (2.5%) и `Auto-TP` (3.5%) fallback | Позиция всегда защищена |
| Неизвестный `ft_order_side` (например `'exit'`) | Авто-фикс на `exit_side` + warning в лог | Без краша |
| TP ордер размещён но парсинг ответа не удался | Обработка `data.orderId` и `data.order.orderId` | TP зарегистрирован в БД |
| `ft_price` = None (отменённый маркет ордер) | Цепочка fallback: `price → order.price → order.average → 0.0` | Без `IntegrityError` |
| Орфан-сделка (нет позиции на бирже) | 3-tier P&L Recovery, затем принудительное закрытие | Статистика сохранена, авто-очистка |
| Reconcile пытается TP/SL для мёртвой позиции | BingX возвращает `101290` (Reduce Only) / `109420` (позиция не существует) → логируется, пропускается | Без краша |
| TP отменён по таймауту | `unfilledtimeout.exit = 525600` (365 дней) | TP остаётся активным |
| Отказ прокси/сети | Worker ловит исключения, ретраит, возвращает сигнал в `pending` | Устойчив |

---

## Справочник по конфигурации

### Конфиг-файлы (загружаются по порядку)

1. `user_data/config.json` — базовый конфиг (биржа, ставка, пары)
2. `user_data/config_vst.json` — настройки VST/sandbox режима
3. `user_data/config_signal.json` — настройки для сигналов

### Переменные окружения

| Переменная | Назначение |
|-----------|-----------|
| `FREQTRADE__EXCHANGE__KEY` | API ключ BingX |
| `FREQTRADE__EXCHANGE__SECRET` | API секрет BingX |
| `FREQTRADE__TELEGRAM__TOKEN` | Токен Telegram бота (уведомления) |
| `FREQTRADE__TELEGRAM__CHAT_ID` | Chat ID для уведомлений |
| `TELEGRAM_API_ID` | Telethon API ID (listener сигналов) |
| `TELEGRAM_API_HASH` | Telethon API hash |
| `TELEGRAM_SIGNALS_CHANNEL_ID` | ID канала-источника сигналов |

### Docker

- `docker-compose.yml` — bind mount: `./user_data:/freqtrade/user_data`
- `Dockerfile.socks` — установка поддержки SOCKS прокси
- Entrypoint: `run_freqtrade_with_auth.sh` (preflight авторизация Telethon)

---

## Развёртывание

### Файлы которые должны быть синхронизированы между хостом и сервером:

| Файл | В Git? | Метод деплоя |
|------|--------|-------------|
| `freqtrade/signals/worker.py` | ✅ | `git pull` + `docker build` |
| `freqtrade/persistence/trade_model.py` | ✅ | `git pull` + `docker build` |
| `user_data/strategies/SignalOnlyStrategy.py` | ✅ | `git pull` (bind mount) |
| `user_data/trades_signals.sqlite` | ❌ (.gitignore) | **Ручной `scp`** |

### Процедура деплоя

```bash
# На сервере:
cd /opt/stacks/freqtrade

# 1. Подтянуть код
git pull

# 2. Если нужно обновить БД (СНАЧАЛА ОСТАНОВИТЬ БОТА!):
docker compose down
scp user@host:/path/to/trades_signals.sqlite ./user_data/trades_signals.sqlite

# 3. Пересобрать и запустить
docker compose build --no-cache
docker compose up -d

# 4. Мониторинг
docker compose logs -f freqtrade
```

> ⚠️ **ВАЖНО**: `*.sqlite` в `.gitignore`. База данных НИКОГДА не синхронизируется через git. Всегда используйте `scp` для переноса БД.

### Чеклист перед деплоем

- [ ] Бот остановлен на сервере
- [ ] БД забэкаплена на сервере (`cp trades_signals.sqlite trades_signals.sqlite.bak_$(date +%Y%m%d)`)
- [ ] Нет открытых сделок которые будут потеряны (`SELECT * FROM trades WHERE is_open=1`)
- [ ] Код закоммичен и запушен
- [ ] БД перенесена если нужно

---

## Устранение неполадок

### "ValueError: Unknown order type: limit"
- **Причина**: Ордер в БД имеет `ft_order_side='exit'` (legacy значение)
- **Исправление**: Авто-фикс в `trade_model.py` → маппинг на `exit_side` с warning в лог
- **Предотвращение**: Все новые ордера используют `trade.exit_side` (`'sell'`/`'buy'`)

### "Not enough X in wallet to exit Trade"
- **Причина**: Сделка открыта в БД но позиция не существует на бирже
- **Исправление**: `handle_onexchange_order()` найдёт и закроет ордер автоматически
- **Если не помогает**: Закрыть сделку в БД вручную:
  ```sql
  UPDATE trades SET is_open=0, exit_reason='manually_closed', close_date=datetime('now') WHERE id=<trade_id>;
  ```

### "BINGX RECONCILE: TP Error ... Reduce Only order cannot open position"
- **Причина**: Попытка разместить TP для позиции которая не существует на бирже
- **Исправление**: Reconcile теперь пропускает орфан-сделки (нет позиции + нет ордеров → `continue`)

### "Failed to get orderId from TP response"
- **Причина**: BingX вернул orderId в неожиданном формате
- **Исправление**: Код обрабатывает и `data.orderId` и `data.order.orderId`

### Telethon "Connection reset by peer"
- **Причина**: Telegram-сервер закрыл MTProto соединение (нормальное поведение)
- **Исправление**: Telethon переподключается автоматически. Действий не требуется.

### "fetch_order() RequestTimeout"
- **Причина**: BingX VST API тормозит или ID ордера больше не существует
- **Исправление**: Автоматический ретрай (до 5 раз). Если постоянно — ордер мог истечь на бирже.

---

## История изменений

### 2026-05-06: Фикс стабильности (Критический)

**Проблема**: Краш-цикл из-за устаревших данных в БД и багов управления ордерами.

**Корневые причины**:
1. `ft_order_side='exit'` в БД → `ValueError` в `update_trade()`
2. TP ордера размещены на бирже но не зарегистрированы в БД (баг парсинга ответа)
3. Reconcile проверял `'exit'` вместо `trade.exit_side`
4. `unfilledtimeout.exit=1440` (24ч) → Freqtrade авто-отменял TP ордера
5. Reconcile пытался TP/SL для орфан-сделок без позиции

**Применённые исправления**:
- `trade_model.py`: Авто-фикс неизвестного `ft_order_side` вместо краша
- `trade_model.py`: Цепочка fallback для `ft_price` для предотвращения `IntegrityError`
- `worker.py`: Обработка вложенного `data.order.orderId` в ответе BingX
- `worker.py`: Регистрация TP ордеров в БД сразу после размещения
- `worker.py`: Удалён мёртвый вызов `_reconcile_tp_orders()`
- `SignalOnlyStrategy.py`: Reconcile использует `trade.exit_side` для проверки TP
- `SignalOnlyStrategy.py`: Reconcile пропускает орфан-сделки
- `SignalOnlyStrategy.py`: `unfilledtimeout.exit` = 525600 (365 дней)

**Проверено**: Сделка AVAX (Trade 29) прошла полный цикл: вход → TP установлен → SL установлен → сигнал TP → выход → +24.6% прибыли. Ноль ошибок.
