"""
Legacy: parse **post-line** channel exports (сторонний экспортёр, не MTProto).

Основной путь для форка — :mod:`freqtrade.signals.telethon_message` (JSON ``Message.to_dict()``).

Export format (one message per line)::
    DD-MM-YYYY HH:MM:SS | <meta> | <meta> | <message text>

First line may be a header: ``Период: ...`` — skipped.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

EXPORT_HEADER_PREFIX = "Период:"
FIELD_SEPARATOR = " | "


@dataclass(frozen=True, slots=True)
class SignalIngestEvent:
    """
    One unit of ingest — same role as a future Telethon NewMessage payload
    (text the parser sees + time + stable id for replay).
    """

    source: str
    text: str
    occurred_at: datetime
    idempotency_key: str
    replay_line_number: int
    raw_line: str


def _replay_idempotency_key(line_number: int, occurred_at: datetime, text: str) -> str:
    payload = f"{line_number}\n{occurred_at.isoformat()}\n{text}".encode()
    digest = hashlib.sha256(payload).hexdigest()[:24]
    return f"replay:{line_number}:{digest}"


def parse_history_export_line(line: str, line_number: int) -> SignalIngestEvent | None:
    """
    Parse a single line. Returns None for blanks, header, or malformed rows.
    """
    stripped = line.rstrip("\r\n")
    if not stripped.strip():
        return None
    if stripped.startswith(EXPORT_HEADER_PREFIX):
        return None
    if FIELD_SEPARATOR not in stripped:
        return None
    parts = stripped.split(FIELD_SEPARATOR, 3)
    if len(parts) < 4:
        return None
    dt_raw, _meta1, _meta2, text = parts
    try:
        occurred_at = datetime.strptime(dt_raw.strip(), "%d-%m-%Y %H:%M:%S")
    except ValueError:
        return None
    text = text.strip()
    if not text:
        return None
    return SignalIngestEvent(
        source="replay",
        text=text,
        occurred_at=occurred_at,
        idempotency_key=_replay_idempotency_key(line_number, occurred_at, text),
        replay_line_number=line_number,
        raw_line=stripped,
    )


def iter_history_export_events(path: Path | str, *, encoding: str = "utf-8") -> Iterator[SignalIngestEvent]:
    """Yield :class:`SignalIngestEvent` for each parsable line in the file."""
    p = Path(path)
    with p.open(encoding=encoding, errors="replace") as fh:
        for line_number, line in enumerate(fh, start=1):
            ev = parse_history_export_line(line, line_number)
            if ev is not None:
                yield ev
