import sqlite3
import os

db_path = 'user_data/tradesv3.sqlite'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

print("--- DOGE TRADES ---")
cursor.execute("SELECT id, pair, open_date, close_date, open_rate, close_rate, exit_reason, exit_order_status, amount, stake_amount, enter_tag FROM trades WHERE pair LIKE '%DOGE%'")
rows = cursor.fetchall()
for row in rows:
    print(dict(row))

conn.close()
