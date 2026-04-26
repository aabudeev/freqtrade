#!/usr/bin/env python3
"""
Before ``freqtrade trade``: verify MTProto can read the signals channel (Phase C).

Runs automatically in ``run_freqtrade_with_auth.sh`` after ``preflight_telegram_auth.py``.

Skip entirely:
  SKIP_TELEGRAM_CHANNEL_SMOKE=1

Requires (otherwise skip with 0 — no channel configured):
  TELEGRAM_SIGNALS_CHANNEL_ID
  TELEGRAM_API_ID, TELEGRAM_API_HASH
  Session file (see TELEGRAM_SESSION_PATH)

Env (optional):
  TELEGRAM_CHANNEL_SMOKE_LIMIT — default 20
  TELEGRAM_CHANNEL_SMOKE_MIN_EXPECTED — soft minimum fetched count (default 10); below → warning, not fatal if ≥1
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

log = logging.getLogger("preflight_channel_smoke")


def _session_path() -> str:
    default = os.path.join("user_data", ".secrets", "telegram_signals.session")
    return os.path.abspath(os.environ.get("TELEGRAM_SESSION_PATH", default))


def _skip() -> bool:
    return os.environ.get("SKIP_TELEGRAM_CHANNEL_SMOKE", "0").strip() in ("1", "true", "yes")


async def _run() -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    if _skip():
        log.info("SKIP_TELEGRAM_CHANNEL_SMOKE set — skip channel smoke")
        return 0

    ch = os.environ.get("TELEGRAM_SIGNALS_CHANNEL_ID", "").strip()
    if not ch:
        log.info("TELEGRAM_SIGNALS_CHANNEL_ID empty — skip channel smoke")
        return 0

    try:
        api_id = int(os.environ["TELEGRAM_API_ID"])
        api_hash = os.environ["TELEGRAM_API_HASH"]
    except Exception:
        log.error("Channel smoke needs TELEGRAM_API_ID and TELEGRAM_API_HASH")
        return 1

    fetch_limit = int(os.environ.get("TELEGRAM_CHANNEL_SMOKE_LIMIT", "20"))
    min_expected = int(os.environ.get("TELEGRAM_CHANNEL_SMOKE_MIN_EXPECTED", "10"))

    session_path = _session_path()
    if not os.path.isfile(session_path):
        log.error("No Telethon session at %s — run auth / QR first", session_path)
        return 1

    from telethon import TelegramClient

    from freqtrade.signals.channel_smoke import count_channel_smoke
    from freqtrade.signals.telethon_proxy import telethon_proxy_from_env

    proxy = telethon_proxy_from_env()
    client = TelegramClient(session_path, api_id, api_hash, proxy=proxy)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            log.error("Telethon session not authorized — complete QR / preflight auth first")
            return 1

        raw, ingestable = await count_channel_smoke(client, ch, fetch_limit=fetch_limit)
    finally:
        await client.disconnect()

    if raw == 0:
        log.error("Channel smoke failed: 0 messages fetched (no access or empty history?)")
        return 1

    if ingestable == 0:
        log.error(
            "Channel smoke failed: fetched %s messages but none map to SignalIngestEvent "
            "(need text + PeerChannel in Message.to_dict)",
            raw,
        )
        return 1

    if raw < min_expected:
        log.warning(
            "Channel smoke: fetched %s messages (soft min %s) — OK if channel is new or quiet",
            raw,
            min_expected,
        )
    else:
        log.info("Channel smoke OK: fetched %s messages, %s ingestable text rows", raw, ingestable)

    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
