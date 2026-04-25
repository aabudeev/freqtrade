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

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT OR IGNORE INTO settings (key, value) VALUES ('stake_mode', 'fixed');
INSERT OR IGNORE INTO settings (key, value) VALUES ('stake_fixed_amount', '10');
INSERT OR IGNORE INTO settings (key, value) VALUES ('stake_percentage', '3');
INSERT OR IGNORE INTO settings (key, value) VALUES ('default_leverage', '50');
INSERT OR IGNORE INTO settings (key, value) VALUES ('entry_mode', 'single');
INSERT OR IGNORE INTO settings (key, value) VALUES ('exchange_mode', 'vst');
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

    def claim_pending(self, limit: int = 10) -> list[dict]:
        """
        Забирает до `limit` записей со статусом 'pending' и переводит их в 'processing'.
        Возвращает список словарей-записей.
        """
        now = datetime.now(UTC).replace(tzinfo=None).isoformat()
        with self._lock:
            with self._connect() as con:
                con.row_factory = sqlite3.Row
                rows = con.execute(
                    "SELECT * FROM ingest_queue WHERE status = 'pending' ORDER BY created_at ASC LIMIT ?",
                    (limit,)
                ).fetchall()
                
                if not rows:
                    return []
                
                keys = [r["idempotency_key"] for r in rows]
                placeholders = ",".join("?" for _ in keys)
                con.execute(
                    f"UPDATE ingest_queue SET status = 'processing', updated_at = ? WHERE idempotency_key IN ({placeholders})",
                    [now] + keys
                )
                con.commit()
                return [dict(r) for r in rows]

    def mark_status(self, idempotency_key: str, status: str, error_message: str | None = None) -> None:
        """
        Обновляет статус записи (например, на 'parsed', 'failed', 'sent').
        Сохраняет сообщение об ошибке, если оно передано.
        """
        now = datetime.now(UTC).replace(tzinfo=None).isoformat()
        with self._lock:
            with self._connect() as con:
                con.execute(
                    "UPDATE ingest_queue SET status = ?, error_message = ?, updated_at = ? WHERE idempotency_key = ?",
                    (status, error_message, now, idempotency_key)
                )
                con.commit()


    def get_settings(self) -> dict:
        with self._lock:
            with self._connect() as con:
                con.row_factory = sqlite3.Row
                cur = con.cursor()
                cur.execute("SELECT key, value FROM settings")
                rows = cur.fetchall()
                settings = {}
                for row in rows:
                    key = row['key']
                    val = row['value']
                    # Try to convert to int/float if possible
                    if val.isdigit():
                        val = int(val)
                    else:
                        try:
                            val = float(val)
                        except ValueError:
                            pass
                    settings[key] = val
                return settings

    def update_settings(self, new_settings: dict) -> None:
        with self._lock:
            with self._connect() as con:
                cur = con.cursor()
                for key, val in new_settings.items():
                    cur.execute(
                        "UPDATE settings SET value = ? WHERE key = ?",
                        (str(val), key)
                    )
                con.commit()
