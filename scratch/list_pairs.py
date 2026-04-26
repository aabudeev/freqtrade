import sqlite3
conn = sqlite3.connect('user_data/tradesv3.sqlite')
cursor = conn.cursor()
cursor.execute("SELECT DISTINCT pair FROM trades")
print(cursor.fetchall())
conn.close()
