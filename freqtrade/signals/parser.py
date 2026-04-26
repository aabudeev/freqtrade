import re
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

class SignalType(Enum):
    ENTRY = "ENTRY"
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"

class SignalSide(Enum):
    LONG = "LONG"
    SHORT = "SHORT"

@dataclass
class SignalEvent:
    type: SignalType
    symbol: str
    side: Optional[SignalSide] = None
    entry_range: Optional[Tuple[float, float]] = None
    target: Optional[float] = None
    stop: Optional[float] = None
    leverage: Optional[int] = None

# Entry regex patterns
# Expected:
# LONG or SHORT
# Монета: DOGE
# Вход: 0.150 - 0.155 (or single number)
# Цель: 0.180
# Стоп: 0.140
# Плечо: 10x (optional)
_ENTRY_SIDE_PATTERN = re.compile(r"^.*?(LONG|SHORT)\s*$", re.IGNORECASE | re.MULTILINE)
_ENTRY_SYMBOL_PATTERN = re.compile(r"^.*?Монета:\s*([A-Za-z0-9]+)\s*$", re.IGNORECASE | re.MULTILINE)
_ENTRY_PRICE_PATTERN = re.compile(r"^.*?Вход:\s*(?:от\s*)?([\d\.]+)(?:\s*(?:-|до)\s*([\d\.]+))?\s*$", re.IGNORECASE | re.MULTILINE)
_ENTRY_TARGET_PATTERN = re.compile(r"^.*?Цель:\s*([\d\.]+)\s*$", re.IGNORECASE | re.MULTILINE)
_ENTRY_STOP_PATTERN = re.compile(r"^.*?Стоп:\s*([\d\.]+)\s*$", re.IGNORECASE | re.MULTILINE)
_ENTRY_LEVERAGE_PATTERN = re.compile(r"^.*?Плечо:\s*(?:\d+-)?(\d+)[xх]?\s*$", re.IGNORECASE | re.MULTILINE)

# Exit regex patterns
# Expected: DOGE - тейк ✅ or SUI - стоп
_EXIT_TAKE_PATTERN = re.compile(r"^\s*([A-Za-z0-9]+)\s*-\s*тейк(?:\s*✅)?\s*$", re.IGNORECASE)
_EXIT_STOP_PATTERN = re.compile(r"^\s*([A-Za-z0-9]+)\s*-\s*стоп\s*$", re.IGNORECASE)

def parse_signal_text(text: str) -> Optional[SignalEvent]:
    """
    Parses raw Telegram text into a SignalEvent.
    Returns None if message is not recognized or validation fails.
    """
    if not text:
        return None
        
    text = text.strip()
    
    # Check for Take Profit exit signal
    take_match = _EXIT_TAKE_PATTERN.search(text)
    if take_match:
        symbol = take_match.group(1).upper()
        pair = f"{symbol}/USDT:USDT"
        return SignalEvent(type=SignalType.TAKE_PROFIT, symbol=pair)
        
    # Check for Stop Loss exit signal
    stop_match = _EXIT_STOP_PATTERN.search(text)
    if stop_match:
        symbol = stop_match.group(1).upper()
        pair = f"{symbol}/USDT:USDT"
        return SignalEvent(type=SignalType.STOP_LOSS, symbol=pair)
        
    # Check for Entry signal
    side_match = _ENTRY_SIDE_PATTERN.search(text)
    if side_match:
        side_str = side_match.group(1).upper()
        side = SignalSide.LONG if side_str == "LONG" else SignalSide.SHORT
        
        symbol_match = _ENTRY_SYMBOL_PATTERN.search(text)
        price_match = _ENTRY_PRICE_PATTERN.search(text)
        target_match = _ENTRY_TARGET_PATTERN.search(text)
        stop_match = _ENTRY_STOP_PATTERN.search(text)
        leverage_match = _ENTRY_LEVERAGE_PATTERN.search(text)
        
        if not (symbol_match and price_match and target_match and stop_match):
            logger.warning(f"Incomplete entry signal format. Mandatory fields missing:\n{text}")
            return None
            
        symbol = symbol_match.group(1).upper()
        pair = f"{symbol}/USDT:USDT"
        
        p1 = float(price_match.group(1))
        p2 = float(price_match.group(2)) if price_match.group(2) else p1
        entry_range = (min(p1, p2), max(p1, p2))
        avg_entry = sum(entry_range) / 2.0
        
        target = float(target_match.group(1))
        stop_price = float(stop_match.group(1))
        
        # --- VALIDATION (SAFETY FILTERS) ---
        max_diff_mult = 0.50 # Maximum 50% deviation from entry price
        
        # 1. Logical direction check
        if side == SignalSide.LONG:
            if target <= entry_range[0]:
                logger.error(f"Validation failed: LONG target {target} <= entry {entry_range[0]}")
                return None
            if stop_price >= entry_range[1]:
                logger.error(f"Validation failed: LONG stop {stop_price} >= entry {entry_range[1]}")
                return None
        else: # SHORT
            if target >= entry_range[1]:
                logger.error(f"Validation failed: SHORT target {target} >= entry {entry_range[1]}")
                return None
            if stop_price <= entry_range[0]:
                logger.error(f"Validation failed: SHORT stop {stop_price} <= entry {entry_range[0]}")
                return None

        # 2. Intraday filter (Anomaly detection)
        target_diff = abs(target - avg_entry) / avg_entry
        stop_diff = abs(stop_price - avg_entry) / avg_entry
        
        if target_diff > max_diff_mult:
            logger.error(f"Validation failed: Target {target} is too far ({target_diff*100:.1f}%) from entry")
            return None
        if stop_diff > max_diff_mult:
            logger.error(f"Validation failed: Stop {stop_price} is too far ({stop_diff*100:.1f}%) from entry")
            return None
        # -------------------------------

        leverage = None
        if leverage_match:
            leverage = int(leverage_match.group(1))
            
        return SignalEvent(
            type=SignalType.ENTRY,
            symbol=pair,
            side=side,
            entry_range=entry_range,
            target=target,
            stop=stop_price,
            leverage=leverage
        )
        
    logger.debug(f"Message ignored (not recognized as signal): {text}")
    return None
