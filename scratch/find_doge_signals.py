import sqlite3
conn = sqlite3.connect('user_data/signals_queue.sqlite')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()
cursor.execute("SELECT idempotency_key, text, status, occurred_at FROM ingest_queue WHERE text LIKE '%DOGE%' ORDER BY occurred_at DESC LIMIT 5")
rows = cursor.fetchall()
for row in rows:
    print(dict(row))
conn.close()
