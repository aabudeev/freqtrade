import sqlite3
import os

db_path = "/home/abudeev/Development/CUSTOM/copyCryptoTradeBot/freqtrade/user_data/trades_signals.sqlite"

if not os.path.exists(db_path):
    print(f"DB not found at {db_path}")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

print("Performing AGGRESSIVE cleanup of ALL open orders...")
# Mark all open orders as cancelled/closed to let the strategy's reconciliation loop re-register them correctly from the exchange
cursor.execute("UPDATE orders SET ft_is_open = 0, status = 'cancelled' WHERE ft_is_open = 1")
changes = conn.total_changes
conn.commit()

print(f"Cleaned up {changes} orders. Database is now ready for reconciliation.")
conn.close()
