"""
Map Telethon TL ``Message.to_dict()`` payloads to :class:`SignalIngestEvent`.

Use this for live listener and for replay from JSON dumps (``dump_channel_messages_json.py``).
Raw shape matches Telethon serialization (``_`` type keys), not Bot API ``getUpdates``.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from freqtrade.signals.history_export import SignalIngestEvent


def _parse_message_unix_ts(date_val: Any) -> int | None:
    if isinstance(date_val, int):
        return date_val
    if isinstance(date_val, float):
        return int(date_val)
    if isinstance(date_val, datetime):
        if date_val.tzinfo:
            return int(date_val.timestamp())
        return int(date_val.replace(tzinfo=timezone.utc).timestamp())
    if isinstance(date_val, str):
        s = date_val.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo:
            return int(dt.timestamp())
        return int(dt.replace(tzinfo=timezone.utc).timestamp())
    return None


def message_dict_to_ingest_event(
    msg: Mapping[str, Any],
    *,
    default_channel_id: int | None = None,
) -> SignalIngestEvent | None:
    """
    Convert one Telethon ``Message.to_dict()`` object.

    Skips non-``Message`` TL types, empty text, and rows where ``channel_id`` cannot be resolved
    (unless ``default_channel_id`` is set).
    """
    if msg.get("_") != "Message":
        return None
    mid = msg.get("id")
    if mid is None:
        return None
    unix_ts = _parse_message_unix_ts(msg.get("date"))
    if unix_ts is None:
        return None
    text = (msg.get("message") or "").strip()
    if not text:
        return None

    peer = msg.get("peer_id") or {}
    ptype = peer.get("_")
    channel_id: int | None = None
    if ptype == "PeerChannel":
        cid = peer.get("channel_id")
        if cid is not None:
            channel_id = int(cid)
    elif ptype == "PeerChat":
        cid = peer.get("chat_id")
        if cid is not None:
            channel_id = int(cid)

    if channel_id is None:
        channel_id = default_channel_id
    if channel_id is None:
        return None

    occurred_at = datetime.fromtimestamp(unix_ts, tz=timezone.utc).replace(tzinfo=None)
    key = f"telegram:{channel_id}:{int(mid)}"
    raw = json.dumps(msg, ensure_ascii=False, sort_keys=True, default=_json_default)

    return SignalIngestEvent(
        source="telegram",
        text=text,
        occurred_at=occurred_at,
        idempotency_key=key,
        replay_line_number=0,
        raw_line=raw,
    )


def _json_default(obj: Any) -> Any:
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def load_telethon_message_dicts(path: Path | str) -> list[dict[str, Any]]:
    """Load a JSON file: either a list of message dicts or ``{\"messages\": [...]}``."""
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict) and "messages" in raw:
        inner = raw["messages"]
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
    raise ValueError(f"Unexpected JSON shape in {p}: need list or {{'messages': [...]}}")


def iter_ingest_events_from_telethon_json(
    path: Path | str,
    *,
    default_channel_id: int | None = None,
) -> Iterator[SignalIngestEvent]:
    """Yield :class:`SignalIngestEvent` for each parseable Telethon message dict in the file."""
    for msg in load_telethon_message_dicts(path):
        ev = message_dict_to_ingest_event(msg, default_channel_id=default_channel_id)
        if ev is not None:
            yield ev
