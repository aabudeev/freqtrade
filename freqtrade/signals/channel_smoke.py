"""
Live channel smoke: fetch recent MTProto messages and verify ingest mapping.

Used by ``preflight_channel_smoke.py`` on container start (no manual dump).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from freqtrade.signals.telethon_message import message_dict_to_ingest_event

if TYPE_CHECKING:
    from telethon import TelegramClient

log = logging.getLogger(__name__)


def resolve_channel_peer_id(channel_id_raw: str) -> int:
    """
    ``TELEGRAM_SIGNALS_CHANNEL_ID`` is usually positive (e.g. 1566432615).
    Telethon ``get_entity`` for broadcast channels uses ``-100<id>``.
    """
    x = int(channel_id_raw.strip())
    if x < 0:
        return x
    return int(f"-100{x}")


async def count_channel_smoke(
    client: TelegramClient,
    channel_id_raw: str,
    *,
    fetch_limit: int,
) -> tuple[int, int]:
    """
    Connect is assumed done. Fetch up to ``fetch_limit`` messages (newest first).

    Returns ``(raw_count, ingestable_count)`` where ingestable = rows that
    :func:`message_dict_to_ingest_event` accepts (text + resolvable channel).
    """
    peer = resolve_channel_peer_id(channel_id_raw)
    entity = await client.get_entity(peer)
    raw = 0
    ingestable = 0
    async for msg in client.iter_messages(entity, limit=fetch_limit):
        raw += 1
        try:
            d: dict[str, Any] = msg.to_dict()
        except Exception:
            log.exception("message.to_dict failed for id=%s", getattr(msg, "id", "?"))
            continue
        if message_dict_to_ingest_event(d) is not None:
            ingestable += 1
    return raw, ingestable
