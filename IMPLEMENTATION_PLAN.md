# План реализации: BingX futures в форке + прослойка сигналов Telegram

> **Для нового агента / после долгого перерыва:** сначала прочитать **`PROJECT_CONTEXT.md`**, **`PROJECT_SESSION_AND_ROADMAP.md`**, затем **этот файл**. Здесь — пошаговый чеклист с тестами и точками коммита в Git.

## Легенда

- `[ ]` — не сделано  
- `[x]` — сделано  
- **Тест:** критерий «готово к следующему шагу»  
- **Git:** обязательная фиксация (коммит или лёгкий тег `milestone-*`); в коммите указывать фазу, например `feat(bingx): B.2 leverage tiers`  

Секреты (`.env`, `user_data/config.json` с реальными ключами) **не коммитить**.

---

## Фаза 0 — Базовая линия репозитория

**Цель:** одинаковое понимание веток, воспроизводимый смок, пустая основа под фичи.

| # | Задача | Тест | Git |
|---|--------|------|-----|
| [x] | **0.1** Зафиксировать рабочую ветку форка (например `stack/local-docker` или `develop`) как базу; кратко описать в `PROJECT_CONTEXT.md` актуальную ветку | *Подтверждено:* клон на сервер, перенос creds, перезапуск контейнера | опционально |
| [x] | **0.2** Убедиться, что Docker-стек поднимается: `docker compose build && docker compose up -d`, веб + Telegram без падений | Веб, бот, сделки открытие/закрытие OK | — |
| [x] | **0.3** Регрессия смока BingX swap **вне** Freqtrade: `scripts/bingx_swap_smoke_trade.py --demo` (dry) и при необходимости `--demo --execute` на VST | 4 операции смока успешно (ранее) | **commit:** `chore: verify bingx smoke baseline` |
| [x] | **0.4** В README форка или в `PROJECT_SESSION_AND_ROADMAP.md` — одна строка-ссылка: «живой план — `IMPLEMENTATION_PLAN.md`» | §10 roadmap ссылается на план | **commit:** `docs: link implementation plan` |

---

## Фаза B — Поддержка BingX **USDT-M (swap/futures)** в движке Freqtrade (path B)

**Цель:** в режиме `trading_mode: futures` Freqtrade с биржей `bingx` может грузить рынки swap, выставлять/снимать ордера, учитывать hedge/one-way, согласованно с Bitget/Bybit-паттернами в коде.

### B.1 Анализ и дизайн (без большого кода)

| # | Задача | Тест | Git |
|---|--------|------|-----|
| [x] | **B.1.1** Снять «снимок» текущего `freqtrade/exchange/bingx.py` и базового `Exchange`: что именно режет futures (`_supported_trading_mode_margin_pairs`, докстринги, `validate_trading_mode_and_margin_mode` и т.д.) | Файл **`BINGX_FUTURES_GAP_ANALYSIS.md`** §1–2 | **commit:** `docs(bingx): upstream futures gap analysis` |
| [x] | **B.1.2** Сравнить с **Bitget** и **Bybit** (и при необходимости **Gate**): `_ft_has_futures`, leverage tiers, `stoploss_on_exchange`, нюансы hedge | Таблица в **`BINGX_FUTURES_GAP_ANALYSIS.md`** §3 | тот же коммит или следующий |
| [x] | **B.1.3** Решить минимальный scope v1: только **USDT-M linear swap**; **standard futures** BingX явно вне scope (как в смок-скрипте) | **`BINGX_FUTURES_GAP_ANALYSIS.md`** §4 | **commit:** `docs(bingx): scope v1 swap only` |

### B.2 Реализация класса биржи

| # | Задача | Тест | Git |
|---|--------|------|-----|
| [x] | **B.2.1** Расширить `Bingx`: `_supported_trading_mode_margin_pairs` для **futures + isolated/cross** (как у эталонных бирж) | SPOT + **FUTURES+ISOLATED**; CROSS закомментирован до проверки | **commit:** `feat(bingx): enable futures trading modes` |
| [x] | **B.2.2** Добавить/скорректировать `_ft_has` / `_ft_has_futures` (лимиты свечей, типы ордеров, `stoploss`, `order_time_in_force`, пр.) по результатам CCXT и доков BingX | Минимальный `_ft_has_futures` + тесты `tests/exchange/test_bingx.py` | **commit:** `feat(bingx): ft_has for futures` |
| [x] | **B.2.3** Плечи: загрузка **leverage tiers** (если применимо к BingX в Freqtrade), кэш, ошибки API | CCXT: `fetchMarketLeverageTiers`; кэш уже в базовом `load_leverage_tiers`. Парсинг: **`parse_leverage_tier`** заполняет `maxLeverage` (CCXT даёт `None`). Тесты в `test_bingx.py` | **commit:** `feat(bingx): leverage tiers` |
| [x] | **B.2.4** **Hedge vs one-way:** единое поведение с уже отработанным в `bingx_swap_smoke_trade.py` (LONG/SHORT leverage, `hedged` в ордерах при необходимости) | Кэш `fetch_position_mode`; `create_order` обновляет флаг до `_get_params`; `_get_params` → `hedged=True`; `_set_leverage` → `BOTH` или LONG+SHORT. Тесты в `test_bingx.py` | **commit:** `fix(bingx): hedge position mode orders` |
| [x] | **B.2.5** Валидация конфига: пары в формате `BASE/QUOTE:QUOTE`, `trading_mode`, `margin_mode` | `validate_config` → `_validate_bingx_futures_pair_symbols`; whitelist/blacklist; `pytest tests/exchange/test_bingx.py` | **commit:** `fix(bingx): config validation pairs` |

### B.3 Тесты и регрессия

| # | Задача | Тест | Git |
|---|--------|------|-----|
| [x] | **B.3.1** Юнит-тесты в стиле апстрима для `bingx.py` (без сети или с моками) | `pytest tests/exchange/test_bingx.py` | **commit:** `test(bingx): exchange class futures` |
| [x] | **B.3.2** Прогон связанных тестов плагинов/pairlist, если трогали общие пути | *Факт:* менялись `bingx.py` / точечно `freqtradebot.py` — полный прогон pairlist не требовался; регрессия **`pytest tests/exchange/test_bingx.py`**. При будущих правках общих путей — расширить `pytest`. | по необходимости |
| [x] | **B.3.3** Интеграция **VST:** малый сценарий через Freqtrade (dry_run false, demo ключи, минимальный stake) — **опционально** после согласования риска | *2026-04-18:* стек **Docker + live BingX** (не VST): бот поднимается, `load_leverage_tiers (fork)`, FreqUI/WebSocket OK, `initial_state: stopped` до ручного Start. Отдельный прогон **VST** через Freqtrade — по желанию. | **commit:** `test(bingx): vst manual verification` + заметка в roadmap |

### B.4 Закрытие фазы B

| # | Задача | Тест | Git |
|---|--------|------|-----|
| [x] | **B.4.1** Обновить `PROJECT_CONTEXT.md` / roadmap: «BingX futures в форке — да, версия v1» + известные ограничения | Ревью текста | **commit:** `docs: phase B complete` |
| [x] | **B.4.2** Тег релиза форка (опционально): `git tag fork-bingx-futures-v1` | *2026-04-18:* аннотированный тег на текущем `main` | tag **`fork-bingx-futures-v1`**; push: `git push origin fork-bingx-futures-v1` |

---

## Фаза C — Прослойка: Telegram-канал → очередь → исполнение

**Цель:** конвейер от сообщений канала до ордеров BingX swap; **TP/SL из текста сигнала** уходят в **ордера биржи** (не основной режим «крутить цену в коде»); **обязательный SL-watchdog** при пробое стопа без закрытия биржей; состояние сделок в **очереди/БД**; отбивки в **Telegram** и по возможности в **веб** (FreqUI при исполнении через Freqtrade).

**Зависимость:** исполнение через **Freqtrade REST** — после фазы **B**; чистый **CCXT** (path A) — фаза B не блокирует **C.4.1**.

**Архитектура и смысл «демона», тесты без канала, игровой баланс:** см. **`docs/private/phase-c-signals-architecture.md`**.

**Данные проекта:** канал по **`TELEGRAM_SIGNALS_CHANNEL_ID`** в `.env` (см. `docker-compose.yml`); peer Telethon для каналов: **`-100<id>`**. Снимок сообщений для тестов — **`tests/fixtures/signals_channel_messages.json`** (обновлять **`dump_channel_messages_json.py`**); большие локальные дампы не коммитить.

**Статус 2026-04-19:** **C.auth** + **preflight channel smoke** на проде. **C.replay.1–2** — JSON ingest + фикстура. **C.1.1–1.2** + **C.3.1** — фоновый Telethon **listener** в процессе `freqtrade trade` → **`user_data/signals_queue.sqlite`** (`pending`, idempotency). **Дальше:** **C.0** (доки формата), **C.2** (парсер), **C.3.2** (worker), **C.4** (исполнитель).

### Рекомендуемый порядок работ

1. **C.auth** — вход в Telegram **по QR**, сохранение сессии (без опоры на SMS).  
2. **C.replay** — прогон **JSON дампа** `Message.to_dict()` (или сохранённой фикстуры) через тот же контракт, что и live (не ждать сигналов сутками).  
3. **C.0** — форматы входа/выхода/размера + контракт уведомлений (в т.ч. PnL).  
4. **C.1** — live listener на канал.  
5. **C.2** → **C.3** → **C.4** → **C.5** → **C.6**.

### C.auth — Авторизация Telegram (первый практический подэтап)

| # | Задача | Тест | Git |
|---|--------|------|-----|
| [x] | **C.auth.1** Выбрать клиент: **Telethon** (приоритет: встроенный **QR login**) или **Pyrogram** при подтверждённом аналоге | Скрипт **`scripts/signals/telegram_qr_login.py`**, зависимости **`requirements-signals.txt`** | **commit:** `feat(signals): telegram qr login scaffold` |
| [x] | **C.auth.2** `api_id` / `api_hash` только из env; путь к **`*.session`** из env; расширить `.gitignore` (`*.session`, `*.session-journal`) | Повторный запуск без QR при валидной сессии | **commit:** `chore(signals): telegram session env + gitignore` |
| [x] | **C.auth.3** Документ: как обновить сессию при истечении / новом устройстве | Подраздел в **`docs/private/phase-c-signals-architecture.md`** | **commit:** `docs(signals): telegram session ops` |
| [x] | **C.auth.4** Preflight в Docker: **`preflight_telegram_auth.py`** + **`preflight_channel_smoke.py`** (чтение канала при **`TELEGRAM_SIGNALS_CHANNEL_ID`**), **`run_freqtrade_with_auth.sh`**, **`ENABLE_TELEGRAM_SIGNAL_AUTH`**, QR в бот, сессия в `user_data/.secrets/`; compose: **`SKIP_TELEGRAM_CHANNEL_SMOKE`**, лимиты **`TELEGRAM_CHANNEL_SMOKE_*`** | Логи контейнера: auth OK → «Channel smoke OK: fetched …»; при сбое `freqtrade` не стартует | **commit:** `feat(signals): preflight channel smoke` |
| [x] | **C.auth.5** MTProto и прокси: **`telethon_proxy.py`**, **`python-socks[asyncio]`** в **`requirements-signals.txt`**, **`TG_PROXY`** в compose | *2026-04-19:* нет *proxy will be ignored*, коннект к DC | **commit:** `fix(signals): python-socks for telethon proxy` |

### C.replay — Тестовый поток без live-канала

| # | Задача | Тест | Git |
|---|--------|------|-----|
| [x] | **C.replay.1** Контракт события + **сырой поток как у MTProto**: `Message.to_dict()` (JSON) → `SignalIngestEvent`; ключ `telegram:{channel_id}:{message_id}` | **`freqtrade.signals.telethon_message`**, CLI **`scripts/signals/dump_channel_messages_json.py`**, **`replay_telegram_json_dump.py`**; опционально legacy TXT: **`history_export`** / **`replay_history_dump.py`** | **commit:** `feat(signals): telethon json ingest` |
| [x] | **C.replay.2** В репозитории — выборка **реальных** сообщений в `tests/fixtures/signals_channel_messages.json` (обновлять дампером; полный `history_*.txt` не коммитить — в `.gitignore`) | `pytest tests/signals/test_telethon_message.py` | **commit:** `test(signals): telethon json fixtures` |

### C.0 Правила и контракты (документ + типы)

| # | Задача | Тест | Git |
|---|--------|------|-----|
| [x] | **C.0.1** **Формат входа:** LONG/SHORT, монета → `BASE/USDT:USDT`, диапазон входа, цель, стоп, плечо (опереться на реальные строки экспорта) | `docs/signals-format.md` + примеры до/после | **commit:** `docs(signals): entry message format` |
| [x] | **C.0.2** **Формат выхода:** `SYMBOL - тейк ✅`, `SYMBOL - стоп` — сопоставление с **открытой** позицией; v1: без частичных, явные дубликаты/неоднозначность | Таблица кейсов в том же doc | **commit:** `docs(signals): exit rules v1` |
| [x] | **C.0.3** **Размер позиции:** одно правило v1 (% депозита / фикс USDT / из текста) | Запись в doc | **commit:** `docs(signals): sizing v1` |
| [x] | **C.0.4** **Уведомления:** перечень событий (`parsed`, `order_sent`, `filled`, `closed_tp`, `closed_sl`, `error`) и обязательные поля (symbol, side, PnL если закрытие) | Таблица в doc | **commit:** `docs(signals): notification contract` |
| [x] | **C.0.5** **Ордера с уровнями из сигнала:** entry / **TP** / **SL** передаются в API биржи (брекет/триггеры/reduce-only — по возможностям **BingX swap** и CCXT); основной выход по TP/SL — **исполнение на бирже**, не опрос котировок в Python как единственный механизм | Схема ордеров в `docs/signals-format.md` + ссылки на типы BingX | **commit:** `docs(signals): exchange bracket orders` |
| [x] | **C.0.6** **SL-watchdog:** интервал опроса, условие «цена хуже SL при открытой позиции», действие **аварийное закрытие** + событие уведомления | Тот же doc или `phase-c-signals-architecture.md` §исполнение | **commit:** `docs(signals): sl watchdog spec` |

### C.1 Доступ к каналу (live)

| # | Задача | Тест | Git |
|---|--------|------|-----|
| [x] | **C.1.1** Подписка на **NewMessage** для канала (`TELEGRAM_SIGNALS_CHANNEL_ID`); запись в **SQLite** `ingest_queue` (**`SignalQueueStore`**); старт из **`FreqtradeBot`** (daemon thread + Telethon). Отключить: **`ENABLE_TELEGRAM_SIGNALS_LISTENER=0`** | На сервере: новое сообщение в канале → строка в `user_data/signals_queue.sqlite`, лог `Signals queue: enqueued …` | **commit:** `feat(signals): telegram channel listener` |
| [x] | **C.1.2** Тот же код-путь, что **C.replay**: **`message_dict_to_ingest_event`** + тот же **`idempotency_key`** | Реализовано общим вызовом в listener и в replay JSON | **commit:** *(включено в listener)* |

### C.2 Парсер

| # | Задача | Тест | Git |
|---|--------|------|-----|
| [x] | **C.2.1** Сырое сообщение → `SignalEvent` (dataclass / pydantic): entry / exit / ignore | Юнит-тесты на фрагментах из экспорта и фикстуры | **commit:** `feat(signals): parser + tests` |
| [x] | **C.2.2** Логирование «не распарсилось» + счётчик для алертов | Тест на мусорный ввод | **commit:** `feat(signals): parser fallback logging` |

### C.3 Очередь и хранилище

| # | Задача | Тест | Git |
|---|--------|------|-----|
| [x] | **C.3.1** **SQLite** `user_data/signals_queue.sqlite`, таблица **`ingest_queue`**: статусы (v1: записывается `pending`; `processing`/`sent`/`failed` — для worker **C.3.2**), idempotency = **`idempotency_key`**. Модуль **`freqtrade.signals.queue_store`** | `pytest tests/signals/test_queue_store.py` | **commit:** `feat(signals): queue schema` |
| [x] | **C.3.2** Worker: ретраи, backoff, идемпотентность исполнения | Интеграционный тест с локальным Redis/SQLite + mock биржи | **commit:** `feat(signals): queue worker` |

### C.4 Исполнитель

| # | Задача | Тест | Git |
|---|--------|------|-----|
| [ ] | **C.4.1** **Path A:** CCXT BingX swap + логика hedge из `bingx_swap_smoke_trade.py` (общий модуль); выставление **TP/SL на бирже** из распарсенного сигнала (**C.0.5**) | Смок **VST** по событию из replay/очереди | **commit:** `feat(signals): executor ccxt bingx` |
| [x] | **C.4.2** **Path B:** Freqtrade REST `/forceenter` / `/forceexit` — одна «правда» по сделкам для FreqUI (согласовать с тем, как Freqtrade задаёт SL/TP на BingX) | Ручной тест + записи в БД бота | **commit:** `feat(signals): executor freqtrade rpc` |
| [x] | **C.4.3** **Trailing TP (опционально):** если BingX/CCXT поддерживает — вынести в конфиг v1.1; иначе зафиксировать «не поддерживается в MVP» | Док + тест или N/A | **commit:** `feat(signals): trailing tp optional` |
| [x] | **C.4.4** **Реализация SL-watchdog** (**C.0.6**): фоновая задача, принудительное закрытие при пробое SL без закрытия биржей | Мок-тест «SL пропущен» | **commit:** `feat(signals): sl watchdog worker` |

### C.5 Наблюдаемость, веб, «игровой» баланс

| # | Задача | Тест | Git |
|---|--------|------|-----|
| [x] | **C.5.1** Логи без секретов; корреляция `signal_id` / `trade_id` | grep по логам | **commit:** `chore(signals): safe logging` |
| [x] | **C.5.2** **Профиль теста:** отдельный `user_data`/`.env` с **BingX VST** (как `--demo` в смоке), не prod ключи — в FreqUI отображается **биржевой демо-баланс**, а не «внутренний» `dry_run` | Баланс в UI совпадает с ~виртуальным счётом BingX | **commit:** `chore(signals): vst profile docs or compose override` |
| [x] | **C.5.3** **Telegram-отбивки:** бот (Bot API) или заранее выбранный канал; события из **C.0.4** | Тестовое сообщение на каждый тип события | **commit:** `feat(signals): notify telegram` |
| [x] | **C.5.4** **Веб:** журнал **всех** статусов (очередь / открыто / закрыто) в **едином store** + **REST** `/api/v1/signals/...` + страница «Сигналы» (до или вместо доработки FreqUI); FreqUI остаётся для классических сделок бота, но не как единственный источник по каналу | Чеклист в `phase-c-signals-architecture.md` | **commit:** `feat(signals): signals dashboard api + page` |
| [x] | **C.5.5** ~~Опционально: сервис `signal-worker` в `docker compose`~~ **N/A:** SignalWorker запускается как daemon thread внутри `freqtrade trade` (`freqtradebot.py`); использует прямые ссылки на `FreqtradeBot` (wallets, rpc, force_entry/exit) — отдельный контейнер не оправдан | Встроен в основной процесс | — |

### C.6 Закрытие фазы C (MVP)

| # | Задача | Тест | Git |
|---|--------|------|-----|
| [x] | **C.6.1** E2E: **replay** или сообщение в канал → очередь → ордер **VST** → отбивки | **`docs/private/e2e-checklist.md`** — 9 секций, 30+ проверок | **commit:** `docs(signals): e2e checklist done` |
| [x] | **C.6.2** Тег: `signals-mvp-v1` | — | tag |

---

## Фаза D — Укрепление (после MVP)

| # | Задача | Тест | Git |
|---|--------|------|-----|
| [ ] | **D.1** Алерты при `failed` в очереди (Telegram админ-бот / webhook) | Искусственный сбой | по мере надобности |
| [ ] | **D.2** Дедупликация повторных сигналов, TTL на «устаревший» вход | Юнит-тесты | — |
| [x] | **D.3** Синхронизация с позицией на бирже (реконсилиация); расширение логики **C.4.4** (watchdog, расхождения ордеров) | `_reconcile_exchange_positions()` в `SignalWorker`: сверка `failed`/`sent` сигналов с `fetch_positions()` + восстановление застрявших `processing` | **commit:** `feat(signals): exchange position reconciliation` |
| [ ] | **D.4** Кросс-маржа и динамическое плечо: поддержка cross-margin или расчет изолированного плеча так, чтобы цена ликвидации всегда была за стоп-лоссом. | — | — |
| [ ] | **D.5** Динамический риск-менеджмент: вход на % от общего депозита (2-3%), настройка через UI или конфиг вместо фиксированной ставки. | — | — |
| [ ] | **D.6** DCA (усреднение / частичный вход): разбивка начального ордера на части (например, вход частями при уходе цены к стопу). | — | — |
| [ ] | **D.7** Игнорирование потерянных входов: защита от приходящих TP/SL сигналов, для которых мы не входили в позицию. | — | — |
| [ ] | **D.8** Восстановление стейта (Resilience & Offline): безопасный рестарт контейнера. Парсинг истории "пока спали", подхват открытых/закрытых сделок, отбрасывание безнадежно устаревших входов. | — | — |
| [ ] | **D.9** Горячее переключение VST/Live: переключение игрового/реального счета через веб UI (с грациозным перезапуском инстанса Freqtrade). | — | — |

---

## Сводка порядка работ (рекомендуемый)

1. **Фаза 0** — всегда в начале сессии.  
2. **Фаза B** — если цель «всё в Freqtrade + FreqUI».  
3. **Фаза C** — **C.auth → C.replay → C.0 → C.1 …**; **C.4.2 (Freqtrade)** после стабильного **B**; **C.4.1 (CCXT)** можно раньше; для «игрового» баланса в вебе — **C.5.2 (VST-профиль)**.  

Обновляйте чекбоксы (`[ ]` → `[x]`) прямо в этом файле вместе с коммитами — так следующий агент увидит прогресс без угадываний.

---

*Файл — часть форка `aabudeev/freqtrade`; при merge из upstream решать конфликты в пользу сохранения этого документа.*
