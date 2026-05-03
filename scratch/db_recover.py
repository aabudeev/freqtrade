import sqlite3
import os

old_db = "user_data/trades_signals.sqlite"
new_db = "user_data/trades_signals_recovered.sqlite"

if os.path.exists(new_db):
    os.remove(new_db)

try:
    # Try to open the old database
    conn_old = sqlite3.connect(old_db)
    
    # Create a new database
    conn_new = sqlite3.connect(new_db)
    
    # Get all tables
    cursor_old = conn_old.cursor()
    cursor_old.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
    tables = cursor_old.fetchall()
    
    for table_name, create_sql in tables:
        print(f"Recovering table {table_name}...")
        conn_new.execute(create_sql)
        
        # Try to fetch all data from old table
        try:
            cursor_old.execute(f"SELECT * FROM {table_name}")
            rows = cursor_old.fetchall()
            if rows:
                placeholders = ",".join(["?"] * len(rows[0]))
                conn_new.executemany(f"INSERT INTO {table_name} VALUES ({placeholders})", rows)
            conn_new.commit()
        except sqlite3.DatabaseError as e:
            print(f"Error recovering data from {table_name}: {e}. Skipping corrupt rows if any.")
            # If a table is partially corrupt, we might lose some data here, but the DB will be valid.
            continue

    conn_old.close()
    conn_new.close()
    
    # Replace old with new
    os.rename(old_db, old_db + ".bak")
    os.rename(new_db, old_db)
    print("Recovery complete. Old DB backed up to .bak")

except Exception as e:
    print(f"Critical recovery error: {e}")
