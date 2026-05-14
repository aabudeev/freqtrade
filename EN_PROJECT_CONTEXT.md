# Project Context (for AI agent) — do not delete during upstream sync

This file is a **portable memory** for the project. In a new chat: "read all .md files in the project root".

## Project Documentation

| File | Purpose |
|------|---------|
| **`EN/RU_ARCHITECTURE.md`** | Full architecture description: signal flow, trade lifecycle, reconcile, order side mapping, failure modes, deployment, troubleshooting |
| **`EN/RU_PROJECT_CONTEXT.md`** | This file — quick context and links |
| **`EN/RU_SMART_FILTER_PLAN.md`** | Future smart filter plan: BTC Guard, RSI/Bollinger, Volume Filter, Shadow Logging, dashboard |

## Repository and Workflow

- Fork: **https://github.com/aabudeev/freqtrade** (branch `main`)
- Development: **Git push from host → git pull on server** (LattePanda `/opt/stacks/freqtrade`)
- Secrets: only `.env` on machines, **not in Git**
- `*.sqlite` in `.gitignore` — **deploy DB via `scp`**

## Stack

- **Exchange**: BingX (USDT-M perpetual swap, VST sandbox)
- **Proxy**: SOCKS5 via AmneziaWG (`amneziawg2:1080`)
- **Docker**: `docker-compose.yml` + `docker/Dockerfile.socks`
- **Strategy**: `SignalOnlyStrategy` — entries from Telegram channel, not indicators
- **DB**: SQLite (`user_data/trades_signals.sqlite`) — bind mount, not in git

## Current Status (2026-05-14)

- **Bot is stable**: Full cycle ENTRY → TP/SL → EXIT works without errors.
- **Safety mechanisms restored**:
  - Pre-entry spread check (max 2%).
  - `entry_range` check (±0.5% buffer for cross-exchange price differences, up to 10 retries with 1 min interval).
  - Emergency Exit (if current price is already past SL at entry time).
  - Auto SL (2.5%) and TP (3.5%) fallback if missing from signal.
  - Precise P&L for exchange-closed trades (Order History -> Ticker -> Open Rate).
- **Signal Pipeline**: Telegram → parser → worker → exchange → DB → notifications
- **Smart Filter**: To be implemented after 1-2 weeks of stable operation

## Key Code Files

| File | Purpose |
|------|---------|
| `user_data/strategies/SignalOnlyStrategy.py` | Strategy, reconcile logic |
| `freqtrade/signals/worker.py` | SignalWorker: signal processing, TP/SL |
| `freqtrade/signals/telegram_listener.py` | Telethon channel listener |
| `freqtrade/persistence/trade_model.py` | Trade/Order ORM (auto-fix ft_order_side) |
| `freqtrade/exchange/bingx.py` | BingX adapter (leverage tiers, sandbox) |

## Commands

```bash
# Deploy to server
cd /opt/stacks/freqtrade
git pull && docker compose build --no-cache && docker compose up -d
docker compose logs -f freqtrade

# DB — only via scp (not in git!)
scp user_data/trades_signals.sqlite root@LattePanda:/opt/stacks/freqtrade/user_data/
```

Update this file when architecture changes or new components are added.
