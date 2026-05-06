# Контекст проекта (для AI-агента) — не удалять при синхронизации с upstream

Этот файл — **переносимая память** по проекту. В новом чате: «прочитай все .md файлы в корне проекта».

## Документация проекта

| Файл | Назначение |
|------|-----------|
| **`EN/RU_ARCHITECTURE.md`** | Полное описание архитектуры: signal flow, trade lifecycle, reconcile, маппинг сторон ордеров, режимы отказа, деплой, устранение неполадок |
| **`EN/RU_PROJECT_CONTEXT.md`** | Этот файл — краткий контекст и ссылки |
| **`EN/RU_SMART_FILTER_PLAN.md`** | План будущего внедрения умных фильтров: BTC Guard, RSI/Bollinger, Volume Filter, Shadow Logging, дашборд |

## Репозиторий и workflow

- Форк: **https://github.com/aabudeev/freqtrade** (ветка `main`)
- Разработка: **Git push с хоста → git pull на сервере** (LattePanda `/opt/stacks/freqtrade`)
- Секреты: только `.env` на машинах, **не в Git**
- `*.sqlite` в `.gitignore` — **деплой БД через `scp`**

## Стек

- **Биржа**: BingX (USDT-M perpetual swap, VST sandbox)
- **Прокси**: SOCKS5 через AmneziaWG (`amneziawg2:1080`)
- **Docker**: `docker-compose.yml` + `docker/Dockerfile.socks`
- **Стратегия**: `SignalOnlyStrategy` — входы из Telegram-канала, не из индикаторов
- **БД**: SQLite (`user_data/trades_signals.sqlite`) — bind mount, не в git

## Текущий статус (2026-05-06)

- **Бот стабилен**: полный цикл ENTRY → TP/SL → EXIT работает без ошибок
- **Пайплайн сигналов**: Telegram → парсер → worker → биржа → БД → уведомления
- **Smart Filter**: к реализации после 1-2 недель стабильной работы

## Ключевые файлы кода

| Файл | Назначение |
|------|-----------|
| `user_data/strategies/SignalOnlyStrategy.py` | Стратегия, reconcile логика |
| `freqtrade/signals/worker.py` | SignalWorker: обработка сигналов, TP/SL |
| `freqtrade/signals/telegram_listener.py` | Telethon listener канала |
| `freqtrade/persistence/trade_model.py` | Trade/Order ORM (авто-фикс ft_order_side) |
| `freqtrade/exchange/bingx.py` | Адаптер BingX (тиры плечей, sandbox) |

## Команды

```bash
# Деплой на сервер
cd /opt/stacks/freqtrade
git pull && docker compose build --no-cache && docker compose up -d
docker compose logs -f freqtrade

# БД — только через scp (не в git!)
scp user_data/trades_signals.sqlite root@LattePanda:/opt/stacks/freqtrade/user_data/
```

Обновляйте этот файл при изменении архитектуры или добавлении новых компонентов.
