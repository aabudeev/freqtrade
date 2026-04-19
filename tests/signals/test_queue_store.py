# pragma pylint: disable=missing-docstring
from __future__ import annotations

from datetime import datetime, timezone

from freqtrade.signals.history_export import SignalIngestEvent
from freqtrade.signals.queue_store import SignalQueueStore


def _ev(key_suffix: str = "1") -> SignalIngestEvent:
    return SignalIngestEvent(
        source="telegram",
        text="TEST",
        occurred_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None),
        idempotency_key=f"telegram:1:{key_suffix}",
        replay_line_number=0,
        raw_line="{}",
    )


def test_enqueue_inserts(tmp_path):
    st = SignalQueueStore(tmp_path / "q.sqlite")
    assert st.enqueue(_ev("a")) is True
    assert st.pending_count() == 1


def test_enqueue_idempotent(tmp_path):
    st = SignalQueueStore(tmp_path / "q.sqlite")
    ev = _ev("dup")
    assert st.enqueue(ev) is True
    assert st.enqueue(ev) is False
    assert st.pending_count() == 1
