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

def test_claim_pending(tmp_path):
    st = SignalQueueStore(tmp_path / "q.sqlite")
    st.enqueue(_ev("1"))
    st.enqueue(_ev("2"))
    st.enqueue(_ev("3"))
    
    assert st.pending_count() == 3
    
    # Claim 2
    claimed = st.claim_pending(limit=2)
    assert len(claimed) == 2
    assert claimed[0]["idempotency_key"] == "telegram:1:1"
    assert claimed[1]["idempotency_key"] == "telegram:1:2"
    
    assert st.pending_count() == 1
    assert st.count_by_status("processing") == 2
    
    # Claim remaining 1
    claimed2 = st.claim_pending(limit=2)
    assert len(claimed2) == 1
    assert claimed2[0]["idempotency_key"] == "telegram:1:3"
    
    assert st.pending_count() == 0
    assert st.count_by_status("processing") == 3

def test_mark_status(tmp_path):
    st = SignalQueueStore(tmp_path / "q.sqlite")
    st.enqueue(_ev("1"))
    
    # Claim it
    claimed = st.claim_pending(limit=1)
    assert len(claimed) == 1
    
    st.mark_status(claimed[0]["idempotency_key"], "parsed")
    assert st.count_by_status("processing") == 0
    assert st.count_by_status("parsed") == 1
    
    st.mark_status(claimed[0]["idempotency_key"], "failed", "Some error")
    assert st.count_by_status("parsed") == 0
    assert st.count_by_status("failed") == 1

