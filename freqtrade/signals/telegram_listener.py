"""
Background Telethon listener: NewMessage from signals channel → :class:`SignalQueueStore`.

Runs in a daemon thread with its own asyncio loop (same pattern as ExternalMessageConsumer).
"""
from __future__ import annotations

import asyncio
import logging
import os
from threading import Thread
from typing import TYPE_CHECKING

from freqtrade.signals.channel_smoke import resolve_channel_peer_id
from freqtrade.signals.queue_store import SignalQueueStore
from freqtrade.signals.telethon_message import message_dict_to_ingest_event
from freqtrade.signals.telethon_proxy import telethon_proxy_from_env

if TYPE_CHECKING:
    from telethon import TelegramClient

    from freqtrade.constants import Config

logger = logging.getLogger(__name__)


def telegram_signals_listener_enabled() -> bool:
    if os.environ.get("ENABLE_TELEGRAM_SIGNALS_LISTENER", "1").strip().lower() in (
        "0",
        "false",
        "no",
    ):
        return False
    if not os.environ.get("TELEGRAM_SIGNALS_CHANNEL_ID", "").strip():
        return False
    return True


def _session_path() -> str:
    default = os.path.join("user_data", ".secrets", "telegram_signals.session")
    return os.path.abspath(os.environ.get("TELEGRAM_SESSION_PATH", default))


class TelegramSignalsListener:
    def __init__(self, config: Config) -> None:
        self._config = config
        db_path = config["user_data_dir"] / "signals_queue.sqlite"
        self._store = SignalQueueStore(db_path)
        self._thread: Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: TelegramClient | None = None
        self._running = False

    @property
    def store(self) -> SignalQueueStore:
        return self._store

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running = True
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._thread_main, name="telegram-signals-listener", daemon=True)
        self._thread.start()
        logger.info("Telegram signals listener thread started")

    def _thread_main(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_main())
        except Exception:
            logger.exception("Telegram signals listener loop crashed")
        finally:
            if not self._loop.is_closed():
                try:
                    self._loop.close()
                except Exception:
                    pass

    async def _async_main(self) -> None:
        from telethon import TelegramClient, events

        try:
            api_id = int(os.environ["TELEGRAM_API_ID"])
            api_hash = os.environ["TELEGRAM_API_HASH"]
        except Exception:
            logger.error("Telegram signals listener: need TELEGRAM_API_ID and TELEGRAM_API_HASH")
            return

        session_path = _session_path()
        if not os.path.isfile(session_path):
            logger.error("Telegram signals listener: no session at %s", session_path)
            return

        proxy = telethon_proxy_from_env()
        self._client = TelegramClient(session_path, api_id, api_hash, proxy=proxy)
        await self._client.connect()
        if not await self._client.is_user_authorized():
            logger.error("Telegram signals listener: session not authorized")
            await self._client.disconnect()
            return

        ch = os.environ["TELEGRAM_SIGNALS_CHANNEL_ID"].strip()
        peer = resolve_channel_peer_id(ch)
        entity = await self._client.get_entity(peer)

        async def handler(event) -> None:
            try:
                if not event.message:
                    return
                d = event.message.to_dict()
                ev = message_dict_to_ingest_event(d)
                if ev is None:
                    return
                if self._store.enqueue(ev):
                    logger.info("Signals queue: enqueued %s", ev.idempotency_key)
            except Exception:
                logger.exception("Signals queue handler failed")

        assert self._client is not None
        self._client.add_event_handler(handler, events.NewMessage(chats=[entity]))
        logger.info("Telegram signals listener connected (peer %s)", peer)

        # Sync history (last 50 messages) in case the bot was offline
        logger.info("Loading signal history (last 50 messages)...")
        async for msg in self._client.iter_messages(entity, limit=50):
            try:
                if not msg:
                    continue
                d = msg.to_dict()
                ev = message_dict_to_ingest_event(d)
                if ev and self._store.enqueue(ev):
                    logger.info("Signals queue (history): enqueued %s", ev.idempotency_key)
            except Exception:
                logger.exception("Error importing message from history")

        await self._client.run_until_disconnected()

    def shutdown(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive() and self._loop and not self._loop.is_closed():
            fut = asyncio.run_coroutine_threadsafe(self._disconnect(), self._loop)
            try:
                fut.result(timeout=15)
            except Exception as e:
                logger.warning("Telegram signals listener disconnect: %s", e)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=20)
        self._thread = None
        self._loop = None
        self._client = None
        logger.info("Telegram signals listener stopped")

    async def _disconnect(self) -> None:
        if self._client:
            await self._client.disconnect()
