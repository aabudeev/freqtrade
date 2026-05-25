# Signal Copy Trade Bot — Architecture & Operations Guide

> **Last Updated**: 2026-05-06
> **Status**: Stable, running on BingX VST (Sandbox)
> **Strategy**: `SignalOnlyStrategy`

---

## Table of Contents

- [Overview](#overview)
- [System Architecture](#system-architecture)
- [Signal Flow](#signal-flow)
- [Trade Lifecycle](#trade-lifecycle)
- [Key Components](#key-components)
- [Reconciliation Logic](#reconciliation-logic)
- [Order Side Mapping](#order-side-mapping)
- [Known Nuances & Design Decisions](#known-nuances--design-decisions)
- [Failure Modes & Protections](#failure-modes--protections)
- [Configuration Reference](#configuration-reference)
- [Deployment](#deployment)
- [Troubleshooting](#troubleshooting)
- [Change History](#change-history)

---

## Overview

This is a **signal-copying trading bot** built on top of Freqtrade. It listens to a Telegram channel for trade signals (ENTRY, TAKE_PROFIT, STOP_LOSS), opens positions on BingX exchange, and manages TP/SL orders automatically.

The bot does **NOT** use Freqtrade's built-in signal generation (indicators, dataframe analysis). Instead, it relies entirely on external Telegram signals processed by a custom `SignalWorker`.

### Key Characteristics
- **Exchange**: BingX (swap/futures, USDT-margined)
- **Mode**: VST (Virtual/Sandbox Trading) — configurable for live
- **Leverage**: As specified by signal (typically 25x)
- **Stake per trade**: 20 USDT
- **Proxy**: All traffic routes through SOCKS5 proxy (`amneziawg2:1080`)
- **Database**: SQLite (`user_data/trades_signals.sqlite`)

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Docker Container                         │
│                                                             │
│  ┌──────────────────┐    ┌──────────────────────────────┐   │
│  │ TelegramSignals   │    │      SignalWorker             │   │
│  │ Listener          │───▶│  (background thread)          │   │
│  │ (Telethon)        │    │                              │   │
│  │                   │    │  • Parses signals             │   │
│  │ Listens to channel│    │  • Opens trades via RPC       │   │
│  │ -1001566432615    │    │  • Places TP via CCXT API     │   │
│  └──────────────────┘    │  • Sets SL on trade object     │   │
│           │               │  • Network diagnostics         │   │
│           ▼               └──────────┬───────────────────┘   │
│  ┌──────────────────┐                │                       │
│  │ signals.db        │                │ RPC calls             │
│  │ (signal queue)    │                ▼                       │
│  └──────────────────┘    ┌──────────────────────────────┐   │
│                          │      FreqtradeBot              │   │
│                          │  (main loop, every ~5s)        │   │
│                          │                                │   │
│                          │  • manage_open_orders()        │   │
│                          │  • exit_positions()            │   │
│                          │  • bot_loop_start() → Reconcile│   │
│                          │  • handle_onexchange_order()   │   │
│                          └──────────┬───────────────────┘   │
│                                     │                       │
│                          ┌──────────┴───────────────────┐   │
│                          │  trades_signals.sqlite         │   │
│                          │  (trades, orders, custom_data) │   │
│                          └──────────────────────────────┘   │
│                                     │                       │
└─────────────────────────────────────┼───────────────────────┘
                                      │ CCXT / REST API
                                      ▼
                            ┌──────────────────┐
                            │    BingX Exchange  │
                            │  (via SOCKS5 proxy)│
                            └──────────────────┘
```

---

## Signal Flow

### 1. Signal Ingestion

```
Telegram Channel → TelegramSignalsListener → signals.db (SQLite queue)
```

- **Listener** runs in a separate thread using Telethon (MTProto)
- Messages are parsed and stored in `signals.db` with status `pending`
- Signal types: `ENTRY`, `TAKE_PROFIT`, `STOP_LOSS`

### 2. Signal Processing

```
SignalWorker.process_once() → claim_pending() → parse → execute
```

- Worker polls `signals.db` every few seconds
- Claims pending signals and processes them sequentially
- Signal format example:
  ```
  📈 LONG
  ▪️Монета: AVAX
  ▪️Плечо: 25-50х
  ▪️Вход: от 9.118 до 9.4
  ▪️Цель: 9.494
  ▪️Стоп: 8.836
  ```

### 3. Signal Types & Actions

| Signal Type | Action |
|-------------|--------|
| `ENTRY` | Open new trade via `_rpc_force_entry()`, set SL, place TP |
| `TAKE_PROFIT` | Close existing trade via `_rpc_force_exit()` |
| `STOP_LOSS` | Close existing trade via `_rpc_force_exit()` |

---

## Trade Lifecycle

### Entry (ENTRY signal)

1. `SignalWorker` receives ENTRY signal and runs **pre-flight checks**:
   - **Spread Check**: If bid/ask spread > 2%, the trade is skipped (`skipped` permanently).
   - **Entry Range Check**: Current price is compared to the entry range (with ±0.5% buffer for exchange differences). If price is too far out, signal is returned to `pending` (up to 10 retries with 1 min interval).
2. `_rpc_force_entry(pair, price=None, order_type="market")` → Freqtrade opens trade.
3. Freqtrade creates `Trade` + `Order` (ft_order_side=`'buy'` for LONG).
4. Worker sets `trade.stop_loss`, `trade.stoploss`, `trade.stop_loss_pct` (uses 2.5% auto-SL if not in signal).
5. **Emergency Exit Check**: If the current price is *already* past the SL level after entry, an immediate `_rpc_force_exit` is triggered to prevent an unprotected position.
6. Worker places **TP limit order** on exchange via direct CCXT API (uses 3.5% auto-TP if not in signal).
7. TP order is **registered in DB** with `ft_order_side=trade.exit_side` (e.g. `'sell'`).
8. Reconcile (next `bot_loop_start`) places **SL trigger order** on exchange.

### TP Hit (exchange fills TP order)

1. `manage_open_orders()` → `fetch_order()` for each open order
2. If TP order status = `'closed'` → `update_trade_state()` → `trade.update_trade()`
3. `order.ft_order_side == trade.exit_side` → matches → trade closes
4. `trade.close(price)` → `is_open=0`, `close_rate`, `close_profit` set

### SL Hit (exchange fills SL trigger) or Ghost Trade

1. Position disappears from exchange.
2. `exit_positions()` → `check_exit_amount()` → wallet shows 0 balance.
3. `handle_onexchange_order()` → fetches all orders from exchange.
4. Finds filled sell order → creates/updates Order record → `update_trade_state()`.
5. Trade closes with `exit_reason='sold_on_exchange'`.
6. **3-tier P&L Recovery (Ghost Reconcile)**: If Freqtrade missed the position closure (e.g. bot was offline), it tries to recover the exact exit price to preserve statistics:
   - *Tier 1*: Search for the closed order in exchange history.
   - *Tier 2*: Ticker (current price), if order not found.
   - *Tier 3*: Open Rate (breakeven) as a last resort fallback.

### TP/SL Signal from Channel

1. `SignalWorker` receives TAKE_PROFIT or STOP_LOSS signal
2. Finds matching open trade by pair
3. `_rpc_force_exit(trade_id)` → Freqtrade cancels existing orders, places market exit
4. Trade closes with `exit_reason='force_exit'`

---

## Key Components

### Files

| File | Purpose |
|------|---------|
| `user_data/strategies/SignalOnlyStrategy.py` | Strategy class, reconciliation logic, trade exit hooks |
| `freqtrade/signals/worker.py` | SignalWorker: processes signals, places TP/SL, network diagnostics |
| `freqtrade/signals/telegram_listener.py` | Telethon-based Telegram channel listener |
| `freqtrade/signals/signal_store.py` | SQLite queue for signals (`signals.db`) |
| `freqtrade/signals/signal_parser.py` | Parses Telegram message text into SignalEvent |
| `freqtrade/persistence/trade_model.py` | Trade/Order ORM models (modified: auto-fix + ft_price fallback) |
| `freqtrade/freqtradebot.py` | Core Freqtrade loop (unmodified except for custom exchange support) |
| `freqtrade/exchange/bingx.py` | BingX exchange adapter (leverage tiers, sandbox mode) |

### SignalOnlyStrategy

```python
# Key settings
minimal_roi = {"0": 10.0}          # Effectively disabled (1000% ROI)
stoploss = -0.99                    # Effectively disabled (99% loss)
trailing_stop = False
use_custom_stoploss = False
use_exit_signal = False
stoploss_on_exchange = False        # SL managed by worker/reconcile, not FT core

unfilledtimeout = {
    'entry': 10,                    # Cancel unfilled entries after 10 min
    'exit': 525600,                 # 365 days — TP orders must NOT be auto-cancelled
}
```

### SignalWorker

- Runs in a **separate thread** (`threading.Thread`, daemon=True)
- Processes signals from `signals.db`
- Places TP orders via **direct CCXT API** (`swapV2PrivatePostTradeOrder`)
- Registers TP orders in Freqtrade DB (`Order` model)
- Runs network diagnostics every 10 minutes
- Handles SL price validation against liquidation price

---

## Reconciliation Logic

Reconciliation runs in `SignalOnlyStrategy.bot_loop_start()`, throttled to every 5 minutes (`_reconcile_interval = 300`).

### What it does:

1. **Fetches exchange state** for each open trade:
   - Open orders (CCXT `fetch_open_orders`)
   - Pending/trigger orders (BingX raw API `swapV2PrivateGetTradePendingOrders`)
   - Positions (from `fetch_positions`)

2. **Orphan detection**: If trade has no position AND no orders on exchange:
   - Logs warning: `"Trade X appears to have NO position and NO orders"`
   - **Skips** TP/SL placement (`continue`)
   - `handle_onexchange_order()` in the main loop will close it

3. **TP reconciliation**: If no TP order found in DB (`ft_order_side == trade.exit_side` and `ft_is_open`):
   - Scans exchange orders for matching TP (same side, price within 0.5%)
   - If found → registers in DB
   - If not found → places new TP limit order on exchange

4. **SL reconciliation**: If no SL order found in DB (`ft_order_side == 'stoploss'` and `ft_is_open`):
   - Scans pending orders for matching SL (STOP_MARKET type)
   - If found → registers in DB
   - If not found → places new SL trigger order on exchange

### BingX TP Response Parsing

BingX returns TP order IDs in a nested structure:
```json
{"data": {"order": {"orderId": 2051746376414400512}}}
```

The code handles both `data.orderId` and `data.order.orderId` formats.

---

## Order Side Mapping

**Critical**: Freqtrade's `update_trade()` validates `ft_order_side` against these values:

| Trade Direction | `entry_side` | `exit_side` | SL side |
|----------------|-------------|------------|---------|
| LONG | `'buy'` | `'sell'` | `'stoploss'` |
| SHORT | `'sell'` | `'buy'` | `'stoploss'` |

**Never use `'exit'` as `ft_order_side`.** This was a legacy bug that caused `ValueError` crash loops.

If an unknown `ft_order_side` is encountered, `trade_model.py` now **auto-fixes** it:
```python
# Instead of crashing:
# raise ValueError(f"Unknown order type: {order.order_type}")

# Now auto-fixes:
logger.warning(f"Unknown ft_order_side '{order.ft_order_side}' ... Mapping to exit_side")
order.ft_order_side = self.exit_side
```

---

## Known Nuances & Design Decisions

### 1. `stoploss_on_exchange: False`
- Freqtrade does NOT manage SL orders through its standard mechanism
- SL is placed by `SignalWorker` (sets `trade.stop_loss`) and `reconcile` (places STOP_MARKET on exchange)
- When SL triggers, `handle_onexchange_order()` detects the closed position and closes the trade
- This is **by design** — Freqtrade's SL mechanism conflicts with our signal-based SL prices

### 2. SL Capping at Liquidation
- If signal SL is beyond liquidation price, worker caps it:
  ```
  Signal SL 8.836 is beyond liquidation 9.04184. Capping SL.
  ```
- Formula: `liquidation ± 3%` buffer

### 3. Amount Mismatch Warnings (BingX swap)
- CCXT sets `contractSize=1` for BingX swaps while some `fetch_my_trades` fills use contract-step `qty` (`tradeMinQuantity` in `market.info`), not base coin amount.
- Symptom: `WARNING: Amount X does not match amount Y` (e.g. SOL `0.12` vs `4.19`, DOGE `70480` vs `3524`).
- **Fork fix:** `freqtrade/exchange/bingx.py` — `_trades_contracts_to_amount` scales qty-only fills; `_order_contracts_to_amount` leaves `origQty`/`executedQty` unchanged (already base units).
- After the fix, fee sync and `close_profit` should align with the exchange; do not ignore the warning.

### 4. Threading Model
- `SignalWorker` runs in a separate thread from `FreqtradeBot` main loop
- Both access the same SQLAlchemy session
- At current signal volume (few per day), race conditions are extremely unlikely
- If volume increases significantly, consider `scoped_session`

### 5. Network/Proxy
- All traffic routes through SOCKS5 proxy (`amneziawg2:1080` via WireGuard/AmneziaWG)
- Worker runs network diagnostics every 10 minutes
- If proxy drops: API calls timeout → worker retries → signals stay in `pending` state
- Telethon reconnects automatically on connection reset

### 6. `fetch_order()` RequestTimeout for Old Orders
- BingX VST API occasionally times out when fetching very old order IDs
- These are retried (up to 5 times) and typically resolve
- If persistent: indicates the order no longer exists on exchange

---

## Failure Modes & Protections

| Failure / Threat | Protection | Result |
|---------|-----------|--------|
| High volatility / low liquidity | Pre-entry spread check (max 2%) | Signal skipped |
| Price outside entry range (slippage) | `entry_range` check with ±0.5% buffer | Retry up to 10m, then `skipped` |
| Trade opened but price already breached SL | `Emergency Exit` right after entry | Market exit, prevents loss |
| Signal missing SL or TP | `Auto-SL` (2.5%) and `Auto-TP` (3.5%) fallback | Position is always protected |
| Unknown `ft_order_side` (e.g. `'exit'`) | Auto-fix to `exit_side` + warning log | No crash |
| TP order placed but response parsing fails | Handles both `data.orderId` and `data.order.orderId` | TP registered in DB |
| `ft_price` is None (canceled market order) | Fallback chain: `price → order.price → order.average → 0.0` | No `IntegrityError` |
| Orphan trade (no position on exchange) | 3-tier P&L Recovery, then forced close | Stats preserved, auto-cleanup |
| Reconcile tries TP/SL for dead position | BingX returns `101290` (Reduce Only) / `109420` (position not exist) → logged, skipped | No crash |
| TP cancelled by timeout | `unfilledtimeout.exit = 525600` (365 days) | TP stays active |
| Proxy/network failure | Worker catches exceptions, retries, returns signal to `pending` | Resilient |

---

## Configuration Reference

### Config Files (loaded in order)

1. `user_data/config.json` — base config (exchange, stake, pairs)
2. `user_data/config_vst.json` — VST/sandbox mode settings
3. `user_data/config_signal.json` — signal-specific settings

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `FREQTRADE__EXCHANGE__KEY` | BingX API key |
| `FREQTRADE__EXCHANGE__SECRET` | BingX API secret |
| `FREQTRADE__TELEGRAM__TOKEN` | Bot notifications Telegram token |
| `FREQTRADE__TELEGRAM__CHAT_ID` | Bot notifications chat ID |
| `TELEGRAM_API_ID` | Telethon API ID (signal listener) |
| `TELEGRAM_API_HASH` | Telethon API hash |
| `TELEGRAM_SIGNALS_CHANNEL_ID` | Signal source channel ID |

### Docker

- `docker-compose.yml` — bind mount: `./user_data:/freqtrade/user_data`
- `Dockerfile.socks` — installs SOCKS proxy support
- Entrypoint: `run_freqtrade_with_auth.sh` (handles Telethon auth preflight)

---

## Deployment

### Files that must be in sync between host and server:

| File | In Git? | Deploy Method |
|------|---------|---------------|
| `freqtrade/signals/worker.py` | ✅ | `git pull` + `docker build` |
| `freqtrade/persistence/trade_model.py` | ✅ | `git pull` + `docker build` |
| `user_data/strategies/SignalOnlyStrategy.py` | ✅ | `git pull` (bind mount) |
| `user_data/trades_signals.sqlite` | ❌ (.gitignore) | **Manual `scp`** |

### Deploy Procedure

```bash
# On server:
cd /opt/stacks/freqtrade

# 1. Pull code
git pull

# 2. If DB needs updating (STOP BOT FIRST!):
docker compose down
scp user@host:/path/to/trades_signals.sqlite ./user_data/trades_signals.sqlite

# 3. Rebuild and start
docker compose build --no-cache
docker compose up -d

# 4. Monitor
docker compose logs -f freqtrade
```

> ⚠️ **IMPORTANT**: `*.sqlite` is in `.gitignore`. The database NEVER syncs via git. Always use `scp` for DB transfers.

### Pre-deploy Checklist

- [ ] Bot stopped on server
- [ ] DB backed up on server (`cp trades_signals.sqlite trades_signals.sqlite.bak_$(date +%Y%m%d)`)
- [ ] No open trades that would be lost (check with `SELECT * FROM trades WHERE is_open=1`)
- [ ] Code committed and pushed
- [ ] DB transferred if needed

---

## Troubleshooting

### "ValueError: Unknown order type: limit"
- **Cause**: Order in DB has `ft_order_side='exit'` (legacy value)
- **Fix**: Auto-fixed by `trade_model.py` → maps to `exit_side` with warning log
- **Prevention**: All new orders use `trade.exit_side` (`'sell'`/`'buy'`)

### "Not enough X in wallet to exit Trade"
- **Cause**: Trade is open in DB but position doesn't exist on exchange
- **Fix**: `handle_onexchange_order()` will find and close the order automatically
- **If persistent**: Manually close the trade in DB:
  ```sql
  UPDATE trades SET is_open=0, exit_reason='manually_closed', close_date=datetime('now') WHERE id=<trade_id>;
  ```

### "BINGX RECONCILE: TP Error ... Reduce Only order cannot open position"
- **Cause**: Trying to place TP for a position that doesn't exist on exchange
- **Fix**: Reconcile now skips orphan trades (no position + no orders → `continue`)

### "Failed to get orderId from TP response"
- **Cause**: BingX returned orderId in unexpected format
- **Fix**: Code now handles both `data.orderId` and `data.order.orderId`

### Telethon "Connection reset by peer"
- **Cause**: Telegram server closed the MTProto connection (normal behavior)
- **Fix**: Telethon reconnects automatically. No action needed.

### "fetch_order() RequestTimeout"
- **Cause**: BingX VST API is slow or order ID no longer exists
- **Fix**: Automatic retry (up to 5 times). If persistent, the order may have expired on exchange.

---

## Change History

### 2026-05-06: Stability Fix (Critical)

**Problem**: Crash loop caused by stale DB data and order management bugs.

**Root Causes**:
1. `ft_order_side='exit'` in DB → `ValueError` in `update_trade()`
2. TP orders placed on exchange but not registered in DB (response parsing bug)
3. Reconcile checking for `'exit'` instead of `trade.exit_side`
4. `unfilledtimeout.exit=1440` (24h) → Freqtrade auto-cancelled TP orders
5. Reconcile attempting TP/SL for orphan trades with no position

**Fixes Applied**:
- `trade_model.py`: Auto-fix unknown `ft_order_side` instead of crashing
- `trade_model.py`: `ft_price` fallback chain to prevent `IntegrityError`
- `worker.py`: Handle nested `data.order.orderId` in BingX TP response
- `worker.py`: Register TP orders in DB immediately after placement
- `worker.py`: Remove dead `_reconcile_tp_orders()` call
- `SignalOnlyStrategy.py`: Reconcile uses `trade.exit_side` for TP check
- `SignalOnlyStrategy.py`: Reconcile skips orphan trades
- `SignalOnlyStrategy.py`: `unfilledtimeout.exit` = 525600 (365 days)
- `SignalOnlyStrategy._register_order()`: Uses `trade.exit_side` not `'exit'`
- DB: Fixed 238 orders with invalid `ft_order_side`, closed orphan trades

**Verified**: AVAX trade (Trade 29) completed full lifecycle: entry → TP set → SL set → TP signal → exit → +24.6% profit. Zero errors.
