"""
Background Telethon listener: NewMessage from signals channel → :class:`SignalQueueStore`.

Runs in a daemon thread with its own asyncio loop (same pattern as ExternalMessageConsumer).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
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
        db_path = config["user_data_dir"] / "signals.db"
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
        
        while self._running:
            try:
                self._loop.run_until_complete(self._async_main())
            except Exception:
                # If it crashed, log and retry unless we are shutting down
                if self._running:
                    logger.exception("Telegram signals listener loop crashed. Restarting in 10s...")
                    time.sleep(10)
                else:
                    logger.info("Telegram signals listener loop ended.")
            else:
                # Normal exit from _async_main (e.g. client disconnected voluntarily)
                if self._running:
                    logger.warning("Telegram signals listener disconnected unexpectedly. Reconnecting...")
                    time.sleep(5)
                else:
                    break
        
        # Cleanup loop
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
        
        try:
            await self._client.connect()
            if not await self._client.is_user_authorized():
                logger.error("Telegram signals listener: session not authorized")
                return

            ch = os.environ["TELEGRAM_SIGNALS_CHANNEL_ID"].strip()
            peer = resolve_channel_peer_id(ch)
            entity = await self._client.get_entity(peer)

            async def handler(event) -> None:
                try:
                    if not event.message:
                        return
                    await self._ingest_message(event.message)
                except Exception:
                    logger.exception("Signals queue handler failed")

            self._client.add_event_handler(handler, events.NewMessage(chats=[entity]))
            logger.info("Telegram signals listener connected (peer %s)", peer)

            # Sync history (last 5 messages) on startup
            logger.info("Loading signal history on startup (last 5, max 5m old)...")
            await self._sync_history(entity, limit=5)

            # --- PERIODIC SYNC TASK ---
            # To prevent missing messages during proxy drops
            async def periodic_sync():
                while self._running:
                    await asyncio.sleep(120) # Every 2 minutes
                    try:
                        if self._client and self._client.is_connected():
                            logger.debug("Periodic signal history sync starting...")
                            await self._sync_history(entity, limit=5)
                    except Exception as e:
                        logger.warning(f"Periodic history sync failed: {e}")

            # Start periodic sync as a background task
            sync_task = asyncio.create_task(periodic_sync())
            
            try:
                await self._client.run_until_disconnected()
            finally:
                sync_task.cancel()

        finally:
            if self._client:
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
                self._client = None

    async def _ingest_message(self, msg) -> None:
        from freqtrade.signals.telethon_message import message_dict_to_ingest_event
        from datetime import datetime, timezone
        try:
            d = msg.to_dict()
            ev = message_dict_to_ingest_event(d)
            if not ev:
                return

            # --- AGE FILTER (Max 5 minutes for ENTRY signals) ---
            now = datetime.now(timezone.utc)
            occ = ev.occurred_at
            if occ.tzinfo is None:
                occ = occ.replace(tzinfo=timezone.utc)
            
            age_seconds = (now - occ).total_seconds()
            
            # We skip ENTRY signals older than 5 minutes (300 seconds) entirely.
            # We ALWAYS process TAKE_PROFIT / STOP_LOSS exits, because they might be 
            # needed to close a trade that is still open.
            from freqtrade.signals.parser import parse_signal_text
            parsed_signal = parse_signal_text(ev.text)
            
            if parsed_signal and parsed_signal.type.name == "ENTRY" and age_seconds > 300:
                logger.debug("Skipping old entry signal %s (age %ds)", ev.idempotency_key, age_seconds)
                return

            if self._store.enqueue(ev):
                logger.info("Signals queue: enqueued %s (from %s)", ev.idempotency_key, ev.occurred_at)
        except Exception:
            logger.exception("Error ingesting message")

    async def _sync_history(self, entity, limit=5) -> None:
        # User request: Only scan the last 5 signals.
        async for msg in self._client.iter_messages(entity, limit=limit):
            if not msg:
                continue
            await self._ingest_message(msg)

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
