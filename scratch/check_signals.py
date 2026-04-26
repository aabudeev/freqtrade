import sqlite3
import os

db_path = 'user_data/signals_queue.sqlite'
if not os.path.exists(db_path):
    print(f"File {db_path} not found")
    exit(1)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

print("--- RECENT SIGNALS ---")
cursor.execute("SELECT id, idempotency_key, text, status, occurred_at FROM ingest_queue ORDER BY id DESC LIMIT 10")
rows = cursor.fetchall()
for row in rows:
    print(dict(row))

conn.close()
