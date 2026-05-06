# Development Plan: Smart Signal Filter

This document contains the concept and technical details for improving signal quality. Implementation should only begin after confirming full autonomy and stability of the base bot.

## 1. Implementation Goals
*   **Drawdown Reduction:** Avoiding entries during market crashes.
*   **Winrate Improvement:** Filtering out "junk" signals on illiquid coins or at peak growth.
*   **Automatic Protection:** The bot decides whether entry is safe based on current indicators.

## 2. Main Filtering Modules

### A. BTC Guard (Market Context)
Bitcoin drives the market. If it's falling, altcoin stops will trigger in 90% of cases.
*   **Logic:** Bot checks BTC price change over the last 15-60 minutes.
*   **Threshold:** If BTC dropped > 1.5% in an hour, entry into any LONG signals is blocked.
*   **Status:** Recommended as mandatory.

### B. RSI / Bollinger (FOMO Protection)
Signals often arrive when the price is already overheated.
*   **Logic:** Check RSI indicator (period 14) on 5m and 15m timeframes.
*   **Threshold:** If RSI > 75, entry is prohibited (overbought).
*   **Bollinger:** If price is above the upper Bollinger Band — wait for pullback.

### C. Volume Filter (Liquidity)
Protection against manipulation on low-volume coins.
*   **Logic:** Check 24h trading volume via exchange API.
*   **Threshold:** Minimum volume — $1,000,000. If less — signal is ignored.

---

## 3. Architecture in SignalOnlyStrategy

The filter will be inserted into the `check_signal_for_entry` method (or equivalent) before order execution.

### Example Future Logic (Pseudocode):

```python
def is_signal_safe(self, pair: str) -> bool:
    # 1. Bitcoin check
    if self.get_btc_change_pct() < -1.5:
        logger.warning("SmartFilter: Bitcoin is falling, entry blocked.")
        return False
        
    # 2. Coin overheating check
    rsi = self.get_current_rsi(pair)
    if rsi > 75:
        logger.warning(f"SmartFilter: {pair} is overbought (RSI={rsi}), skipping.")
        return False
        
    # 3. Volume check
    volume = self.get_24h_volume(pair)
    if volume < 1000000:
        logger.warning(f"SmartFilter: Low liquidity on {pair}. Skipping.")
        return False
        
    return True
```

## 4. Monitoring and Analytics (Shadow Logging to DB)

Instead of just writing cancellation reasons to a text log, a separate SQLite database (`signal_stats.sqlite`) should be created to store the bot's "decision history".

### Why is this needed?
Logs with millions of lines are impossible to analyze. A database will allow you to run an SQL query and instantly see filter effectiveness: how many times it saved the deposit, and how many times it cost profit.

### What to record in DB (Table fields):
*   **Timestamp:** Exact signal arrival time.
*   **Pair:** Coin.
*   **Signal_Price:** Price at signal moment.
*   **Hypothetical_TP/SL:** Take profit and stop from the signal.
*   **Decision:** Bot's decision (ENTER / SKIP).
*   **Reason:** Why skipped (e.g., `BTC_DUMP`, `RSI_OVERBOUGHT`).
*   **Indicators_Snapshot:** Values of all indicators at signal moment (RSI, BTC_Change, Volume, Bollinger_Pos).
*   **Market_Context:** BTC price, market volatility (ATR).
*   **Result (filled later):** Whether price reached the hypothetical TP or SL (for analyzing missed profits).

## 5. Implementation Stages (Future)
1.  **Information Mode:** Bot records ALL signals in DB but always enters trades. Needed for accumulating "benchmark" statistics.
2.  **Shadow Filter:** Bot filters trades in reality but continues writing statistics for all signals (including rejected) for analysis.
3.  **Analytical Slice:** After 2-4 weeks, export the table to Excel and see real effectiveness of each indicator.

## 6. Visualization and Dashboard (Real-time UI)

Instead of manual data export, a built-in web page (dashboard) should be implemented that displays data from `signal_stats.sqlite` in real time.

### Main Dashboard Features:
*   **Live Signal Table:** List of all incoming signals with color coding (Green — entered, Gray — filtered).
*   **Efficiency Counter:** Automatic calculation of "Saved Deposit" metric (how many stop losses didn't trigger thanks to the filter).
*   **"Heat" Indicator:** Visual display of current filters (e.g., red BTC indicator if it's falling).
*   **Retro-Analysis:** Ability to click on a filtered signal and see the coin's chart with marks showing where we would have entered and where we would have been stopped out.

## 7. Risks
*   **CPU Load:** Dashboard should request data on-demand, not maintain a permanent heavy connection.
*   **Security:** Access to the statistics page should be protected with the same password as the main FreqUI.

---
**Important:** Return to this file only after the current bot version has run 1-2 weeks without a single failure.
