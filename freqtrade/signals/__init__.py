"""Phase C: signal channel ingest (Telegram replay + live)."""

from freqtrade.signals.history_export import (
    SignalIngestEvent,
    iter_history_export_events,
    parse_history_export_line,
)
from freqtrade.signals.channel_smoke import resolve_channel_peer_id
from freqtrade.signals.queue_store import SignalQueueStore
from freqtrade.signals.telethon_message import (
    iter_ingest_events_from_telethon_json,
    load_telethon_message_dicts,
    message_dict_to_ingest_event,
)
from freqtrade.signals.telethon_proxy import telethon_proxy_from_env

__all__ = [
    "SignalIngestEvent",
    "iter_history_export_events",
    "iter_ingest_events_from_telethon_json",
    "load_telethon_message_dicts",
    "message_dict_to_ingest_event",
    "parse_history_export_line",
    "resolve_channel_peer_id",
    "SignalQueueStore",
    "telethon_proxy_from_env",
]
