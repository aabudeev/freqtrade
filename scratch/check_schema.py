import sqlite3
conn = sqlite3.connect('user_data/signals_queue.sqlite')
cursor = conn.cursor()
cursor.execute("PRAGMA table_info(ingest_queue)")
print(cursor.fetchall())
conn.close()
