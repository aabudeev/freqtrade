# pragma pylint: disable=missing-docstring
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from freqtrade.signals.history_export import SignalIngestEvent
from freqtrade.signals.telethon_message import (
    load_telethon_message_dicts,
    message_dict_to_ingest_event,
    iter_ingest_events_from_telethon_json,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "signals_channel_messages.json"


def test_fixture_exists():
    assert FIXTURE.is_file()


def test_load_wrapped_messages_format(tmp_path: Path):
    inner = [{"_": "Message", "id": 1, "date": 1700000000, "peer_id": {"_": "PeerChannel", "channel_id": 99}, "message": "hi"}]
    p = tmp_path / "w.json"
    p.write_text(json.dumps({"channel_id": 99, "messages": inner}), encoding="utf-8")
    assert len(load_telethon_message_dicts(p)) == 1


def test_message_dict_int_date():
    msg = {
        "_": "Message",
        "id": 100,
        "date": 1704067200,
        "peer_id": {"_": "PeerChannel", "channel_id": 1566432615},
        "message": "TEST coin signal",
    }
    ev = message_dict_to_ingest_event(msg)
    assert ev is not None
    assert ev.source == "telegram"
    assert ev.text == "TEST coin signal"
    assert ev.idempotency_key == "telegram:1566432615:100"
    assert ev.occurred_at == datetime.fromtimestamp(1704067200, tz=timezone.utc).replace(tzinfo=None)
    assert isinstance(ev, SignalIngestEvent)


def test_message_dict_iso_date_string():
    msg = {
        "_": "Message",
        "id": 101,
        "date": "2026-04-19T06:17:20+00:00",
        "peer_id": {"_": "PeerChannel", "channel_id": 1566432615},
        "message": "ISO date row",
    }
    ev = message_dict_to_ingest_event(msg)
    assert ev is not None
    assert ev.idempotency_key == "telegram:1566432615:101"


def test_default_channel_id_when_peer_missing():
    msg = {
        "_": "Message",
        "id": 7,
        "date": 1700000000,
        "peer_id": {"_": "PeerUser", "user_id": 1},
        "message": "fallback channel",
    }
    assert message_dict_to_ingest_event(msg) is None
    ev = message_dict_to_ingest_event(msg, default_channel_id=1566432615)
    assert ev is not None
    assert ev.idempotency_key == "telegram:1566432615:7"


def test_skips_non_message_and_empty_text():
    assert message_dict_to_ingest_event({"_": "MessageService", "id": 1}) is None
    assert (
        message_dict_to_ingest_event(
            {
                "_": "Message",
                "id": 1,
                "date": 1700000000,
                "peer_id": {"_": "PeerChannel", "channel_id": 1},
                "message": "   ",
            }
        )
        is None
    )


def test_fixture_replay_yields_events():
    events = list(iter_ingest_events_from_telethon_json(FIXTURE))
    assert len(events) >= 1
    assert all(ev.source == "telegram" for ev in events)
    assert all(ev.idempotency_key.startswith("telegram:1566432615:") for ev in events)
    keys = {ev.idempotency_key for ev in events}
    assert len(keys) == len(events)


def test_fixture_first_message_stable():
    events = list(iter_ingest_events_from_telethon_json(FIXTURE))
    a, b = events[0].idempotency_key, list(iter_ingest_events_from_telethon_json(FIXTURE))[0].idempotency_key
    assert a == b
