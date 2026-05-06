# Контекст проекта (для AI-агента) — не удалять при синхронизации с upstream

Этот файл — **переносимая память** по проекту. В новом чате: «прочитай все .md файлы в корне проекта».

## Документация проекта

| Файл | Назначение |
|------|-----------|
| **`ARCHITECTURE.md`** | Полное описание архитектуры: signal flow, trade lifecycle, reconcile, order side mapping, failure modes, deployment, troubleshooting |
| **`IMPLEMENTATION_PLAN.md`** | Пошаговый roadmap (фазы 0, B, C — выполнены; **фаза D** — будущие задачи) |
| **`PROJECT_CONTEXT.md`** | Этот файл — краткий контекст и ссылки |
| **`SMART_FILTER_PLAN.md`** | План будущего внедрения умных фильтров: BTC Guard, RSI/Bollinger, Volume Filter, Shadow Logging, дашборд |

## Репозиторий и workflow

- Форк: **https://github.com/aabudeev/freqtrade** (ветка `main`)
- Разработка: **Git push с хоста → git pull на сервере** (LattePanda `/opt/stacks/freqtrade`)
- Секреты: только `.env` на машинах, **не в Git**
- `*.sqlite` и `*.md` в `.gitignore` — **добавлять через `git add -f`**

## Стек

- **Exchange**: BingX (USDT-M perpetual swap, VST sandbox)
- **Proxy**: SOCKS5 через AmneziaWG (`amneziawg2:1080`)
- **Docker**: `docker-compose.yml` + `docker/Dockerfile.socks`
- **Strategy**: `SignalOnlyStrategy` — входы из Telegram-канала, не из индикаторов
- **БД**: SQLite (`user_data/trades_signals.sqlite`) — bind mount, не в git

## Текущий статус (2026-05-06)

- **Бот стабилен**: полный цикл ENTRY → TP/SL → EXIT работает без ошибок
- **Фазы 0, B, C**: выполнены (BingX futures, Telegram signals, reconciliation)
- **Фаза D**: будущие задачи (D.1–D.9 в `IMPLEMENTATION_PLAN.md`)
- **Smart Filter**: к реализации после 1-2 недель стабильной работы

## Ключевые файлы кода

| Файл | Назначение |
|------|-----------|
| `user_data/strategies/SignalOnlyStrategy.py` | Стратегия, reconcile логика |
| `freqtrade/signals/worker.py` | SignalWorker: обработка сигналов, TP/SL |
| `freqtrade/signals/telegram_listener.py` | Telethon listener канала |
| `freqtrade/persistence/trade_model.py` | Trade/Order ORM (auto-fix ft_order_side) |
| `freqtrade/exchange/bingx.py` | BingX adapter (leverage tiers, sandbox) |

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
