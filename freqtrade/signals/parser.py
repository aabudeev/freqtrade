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

# Регулярные выражения для входа
# Ожидается:
# LONG или SHORT
# Монета: DOGE
# Вход: 0.150 - 0.155 (или одно число)
# Цель: 0.180
# Стоп: 0.140
# Плечо: 10x (опционально)
_ENTRY_SIDE_PATTERN = re.compile(r"^\s*(LONG|SHORT)\s*$", re.IGNORECASE | re.MULTILINE)
_ENTRY_SYMBOL_PATTERN = re.compile(r"^\s*Монета:\s*([A-Za-z0-9]+)\s*$", re.IGNORECASE | re.MULTILINE)
_ENTRY_PRICE_PATTERN = re.compile(r"^\s*Вход:\s*([\d\.]+)(?:\s*-\s*([\d\.]+))?\s*$", re.IGNORECASE | re.MULTILINE)
_ENTRY_TARGET_PATTERN = re.compile(r"^\s*Цель:\s*([\d\.]+)\s*$", re.IGNORECASE | re.MULTILINE)
_ENTRY_STOP_PATTERN = re.compile(r"^\s*Стоп:\s*([\d\.]+)\s*$", re.IGNORECASE | re.MULTILINE)
_ENTRY_LEVERAGE_PATTERN = re.compile(r"^\s*Плечо:\s*(\d+)x?\s*$", re.IGNORECASE | re.MULTILINE)

# Регулярные выражения для выхода
# Ожидается: DOGE - тейк ✅ или SUI - стоп
_EXIT_TAKE_PATTERN = re.compile(r"^\s*([A-Za-z0-9]+)\s*-\s*тейк(?:\s*✅)?\s*$", re.IGNORECASE)
_EXIT_STOP_PATTERN = re.compile(r"^\s*([A-Za-z0-9]+)\s*-\s*стоп\s*$", re.IGNORECASE)

def parse_signal_text(text: str) -> Optional[SignalEvent]:
    """
    Парсит сырой текст из Telegram и возвращает SignalEvent.
    Возвращает None, если сообщение не распознано.
    """
    if not text:
        return None
        
    text = text.strip()
    
    # Проверяем на выход (Take Profit)
    take_match = _EXIT_TAKE_PATTERN.search(text)
    if take_match:
        symbol = take_match.group(1).upper()
        pair = f"{symbol}/USDT:USDT"
        return SignalEvent(type=SignalType.TAKE_PROFIT, symbol=pair)
        
    # Проверяем на выход (Stop Loss)
    stop_match = _EXIT_STOP_PATTERN.search(text)
    if stop_match:
        symbol = stop_match.group(1).upper()
        pair = f"{symbol}/USDT:USDT"
        return SignalEvent(type=SignalType.STOP_LOSS, symbol=pair)
        
    # Проверяем на вход
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
            logger.warning(f"Неполный формат сигнала на вход. Не удалось найти все обязательные поля:\n{text}")
            return None
            
        symbol = symbol_match.group(1).upper()
        pair = f"{symbol}/USDT:USDT"
        
        p1 = float(price_match.group(1))
        p2 = float(price_match.group(2)) if price_match.group(2) else p1
        entry_range = (min(p1, p2), max(p1, p2))
        
        target = float(target_match.group(1))
        stop_price = float(stop_match.group(1))
        
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
        
    logger.debug(f"Сообщение проигнорировано (не распознано как сигнал): {text}")
    return None
