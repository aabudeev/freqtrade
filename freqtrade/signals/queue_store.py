"""
SQLite queue for raw signal ingest (Phase C.3.1).

Statuses: pending → processing → sent | failed (processing/sent used by worker later).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from freqtrade.signals.history_export import SignalIngestEvent

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_queue (
  idempotency_key TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  text TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  raw_payload TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingest_status ON ingest_queue(status);
"""


class SignalQueueStore:
    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(SCHEMA)
            con.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, check_same_thread=False, timeout=30.0)

    def enqueue(self, event: SignalIngestEvent) -> bool:
        """
        Insert if ``idempotency_key`` is new. Returns True when a row was inserted.
        """
        now = datetime.now(UTC).replace(tzinfo=None).isoformat()
        occ = event.occurred_at.isoformat()
        with self._lock:
            with self._connect() as con:
                try:
                    con.execute(
                        """
                        INSERT INTO ingest_queue (
                          idempotency_key, source, text, occurred_at, raw_payload,
                          status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                        """,
                        (
                            event.idempotency_key,
                            event.source,
                            event.text,
                            occ,
                            event.raw_line,
                            now,
                            now,
                        ),
                    )
                    con.commit()
                    inserted = True
                except sqlite3.IntegrityError:
                    con.rollback()
                    inserted = False
        if inserted:
            log.debug("Enqueued ingest %s", event.idempotency_key)
        return inserted

    def count_by_status(self, status: str) -> int:
        with self._lock:
            with self._connect() as con:
                row = con.execute(
                    "SELECT COUNT(*) FROM ingest_queue WHERE status = ?",
                    (status,),
                ).fetchone()
        return int(row[0]) if row else 0

    def pending_count(self) -> int:
        return self.count_by_status("pending")
