import re

text = """📈 LONG 
 
▪Монета: DOGE
▪Плечо: 25-50х
▪Вход: от 0.09717 до 0.09425
▪Цель: 0.09814
▪Стоп: 0.09134"""

_ENTRY_SIDE_PATTERN = re.compile(r"^\s*(?:📈|📉)?\s*(LONG|SHORT)\s*$", re.IGNORECASE | re.MULTILINE)
_ENTRY_SYMBOL_PATTERN = re.compile(r"^\s*(?:▪)?\s*Монета:\s*([A-Za-z0-9]+)\s*$", re.IGNORECASE | re.MULTILINE)
_ENTRY_PRICE_PATTERN = re.compile(r"^\s*(?:▪)?\s*Вход:\s*(?:от\s*)?([\d\.]+)(?:\s*(?:-|до)\s*([\d\.]+))?\s*$", re.IGNORECASE | re.MULTILINE)
_ENTRY_TARGET_PATTERN = re.compile(r"^\s*(?:▪)?\s*Цель:\s*([\d\.]+)\s*$", re.IGNORECASE | re.MULTILINE)
_ENTRY_STOP_PATTERN = re.compile(r"^\s*(?:▪)?\s*Стоп:\s*([\d\.]+)\s*$", re.IGNORECASE | re.MULTILINE)
_ENTRY_LEVERAGE_PATTERN = re.compile(r"^\s*(?:▪)?\s*Плечо:\s*(?:\d+-)?(\d+)[xх]?\s*$", re.IGNORECASE | re.MULTILINE)

print("SIDE", _ENTRY_SIDE_PATTERN.search(text))
print("SYMBOL", _ENTRY_SYMBOL_PATTERN.search(text))
print("PRICE", _ENTRY_PRICE_PATTERN.search(text))
print("TARGET", _ENTRY_TARGET_PATTERN.search(text))
print("STOP", _ENTRY_STOP_PATTERN.search(text))
print("LEVERAGE", _ENTRY_LEVERAGE_PATTERN.search(text))
