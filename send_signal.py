#!/usr/bin/env python3
import sys
import sqlite3
import uuid
from datetime import datetime, UTC

def main():
    print("=== Manual Signal Injector ===")
    print("Paste your signal text below (press Ctrl+D or Ctrl+Z on a new line when done):")
    print("-------------------------------------------------------------------------")
    
    try:
        lines = sys.stdin.read().strip()
    except KeyboardInterrupt:
        print("\nCancelled.")
        return

    if not lines:
        print("Error: Signal text cannot be empty.")
        return

    # Path inside the Docker container
    db_path = "user_data/signals.db"
    
    # Generate unique idempotency key
    key = f"manual_{int(datetime.now(UTC).timestamp())}_{uuid.uuid4().hex[:6]}"
    now = datetime.now(UTC).replace(tzinfo=None).isoformat()

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Insert signal into queue
        cursor.execute(
            """
            INSERT INTO ingest_queue (
                idempotency_key, source, text, occurred_at, raw_payload,
                symbol, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                key,
                "manual",
                lines,
                now,
                lines,
                None, # Will be auto-extracted by worker
                now,
                now
            )
        )
        conn.commit()
        conn.close()
        
        print("-------------------------------------------------------------------------")
        print(f"✅ Success! Signal successfully enqueued with key: {key}")
        print("The bot will pick it up and process it within a few seconds.")
    except Exception as e:
        print(f"❌ Error inserting signal into database: {e}")

if __name__ == "__main__":
    main()
