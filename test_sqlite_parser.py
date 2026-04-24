import sqlite3
from freqtrade.signals.parser import parse_signal_text, _ENTRY_SIDE_PATTERN, _ENTRY_SYMBOL_PATTERN, _ENTRY_PRICE_PATTERN, _ENTRY_TARGET_PATTERN, _ENTRY_STOP_PATTERN, _ENTRY_LEVERAGE_PATTERN

conn = sqlite3.connect('/home/abudeev/Development/CUSTOM/copyCryptoTradeBot/freqtrade/user_data/signals_queue.sqlite')
cursor = conn.cursor()
cursor.execute("SELECT text FROM ingest_queue ORDER BY created_at DESC LIMIT 5")
rows = cursor.fetchall()

for idx, (raw_text,) in enumerate(rows):
    print(f"--- Signal {idx} ---")
    print("RAW HEX:", raw_text.encode('utf-8').hex())
    print("TEXT:", repr(raw_text))
    
    side = _ENTRY_SIDE_PATTERN.search(raw_text)
    symbol = _ENTRY_SYMBOL_PATTERN.search(raw_text)
    price = _ENTRY_PRICE_PATTERN.search(raw_text)
    target = _ENTRY_TARGET_PATTERN.search(raw_text)
    stop = _ENTRY_STOP_PATTERN.search(raw_text)
    lev = _ENTRY_LEVERAGE_PATTERN.search(raw_text)
    
    print("SIDE:", side)
    print("SYMBOL:", symbol)
    print("PRICE:", price)
    print("TARGET:", target)
    print("STOP:", stop)
    print("LEV:", lev)
    
    res = parse_signal_text(raw_text)
    print("Result:", res)
