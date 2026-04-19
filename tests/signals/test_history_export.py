# pragma pylint: disable=missing-docstring
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from freqtrade.signals.history_export import (
    SignalIngestEvent,
    iter_history_export_events,
    parse_history_export_line,
)


def test_parse_header_skipped():
    assert parse_history_export_line("Период: 2025-01-01 00:00 - 2026-04-18 23:59\n", 1) is None


def test_parse_entry_short():
    line = (
        "01-01-2025 00:30:02 | 🔒125x | Hardcore | "
        "📈 SHORT    ▪️Монета: LINK ▪️Плечо: 25-50х ▪️Вход: от 20.524 до 21.14 ▪️Цель: 20.319 ▪️Стоп: 21.755\n"
    )
    ev = parse_history_export_line(line, 3)
    assert ev is not None
    assert ev.source == "replay"
    assert ev.occurred_at == datetime(2025, 1, 1, 0, 30, 2)
    assert "LINK" in ev.text
    assert "SHORT" in ev.text
    assert ev.replay_line_number == 3
    assert ev.idempotency_key.startswith("replay:3:")
    assert isinstance(ev, SignalIngestEvent)


def test_parse_take():
    line = "01-01-2025 00:45:26 | 🔒125x | Hardcore | LINK - тейк ✅\n"
    ev = parse_history_export_line(line, 4)
    assert ev is not None
    assert "тейк" in ev.text
    assert ev.occurred_at == datetime(2025, 1, 1, 0, 45, 26)


def test_parse_stop():
    line = "02-01-2025 11:33:04 | 🔒125x | Hardcore | SOL - стоп\n"
    ev = parse_history_export_line(line, 26)
    assert ev is not None
    assert "стоп" in ev.text


def test_parse_long_direction():
    line = (
        "03-01-2025 02:01:10 | 🔒125x | Hardcore | "
        "📈 LONG    ▪️Монета: ETC ▪️Плечо: 25-50х ▪️Вход: от 26.797 до 25.993 ▪️Цель: 27.065 ▪️Стоп: 25.189\n"
    )
    ev = parse_history_export_line(line, 32)
    assert ev is not None
    assert "LONG" in ev.text
    assert "ETC" in ev.text


def test_malformed_no_event():
    assert parse_history_export_line("not a valid line\n", 1) is None
    assert parse_history_export_line("", 1) is None


def test_iter_skips_header(tmp_path: Path):
    p = tmp_path / "h.txt"
    p.write_text(
        "Период: 2025-01-01 00:00 - 2026-04-18 23:59\n"
        "01-01-2025 00:45:26 | 🔒125x | Hardcore | LINK - тейк ✅\n",
        encoding="utf-8",
    )
    events = list(iter_history_export_events(p))
    assert len(events) == 1
    assert "LINK" in events[0].text


def test_idempotency_stable():
    line = "01-01-2025 00:45:26 | 🔒125x | Hardcore | LINK - тейк ✅\n"
    a = parse_history_export_line(line, 10)
    b = parse_history_export_line(line, 10)
    assert a is not None and b is not None
    assert a.idempotency_key == b.idempotency_key


def test_different_line_different_key():
    line = "01-01-2025 00:45:26 | 🔒125x | Hardcore | LINK - тейк ✅\n"
    a = parse_history_export_line(line, 1)
    b = parse_history_export_line(line, 2)
    assert a is not None and b is not None
    assert a.idempotency_key != b.idempotency_key
