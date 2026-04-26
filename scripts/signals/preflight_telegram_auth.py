#!/usr/bin/env python3
"""
Before `freqtrade trade`: ensure Telethon session exists (Phase C).

If ENABLE_TELEGRAM_SIGNAL_AUTH=1:
  - Session valid → optional short ping to bot, exit 0
  - Not authorized → send QR image to Telegram bot (TELEGRAM_TOKEN + chat id), wait for scan

Uses same Bot API as Freqtrade notifications (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID or FREQTRADE__*).
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import sys

log = logging.getLogger("preflight_telegram_auth")


def _enabled() -> bool:
    return os.environ.get("ENABLE_TELEGRAM_SIGNAL_AUTH", "0").strip() == "1"


def _bot_token() -> str:
    return (
        os.environ.get("TELEGRAM_TOKEN", "").strip()
        or os.environ.get("FREQTRADE__TELEGRAM__TOKEN", "").strip()
    )


def _bot_chat_id() -> str:
    v = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("FREQTRADE__TELEGRAM__CHAT_ID") or ""
    return str(v).strip()


def _session_path() -> str:
    default = os.path.join("user_data", ".secrets", "telegram_signals.session")
    return os.path.abspath(os.environ.get("TELEGRAM_SESSION_PATH", default))


def _qr_png(url: str) -> bytes:
    import qrcode

    buf = io.BytesIO()
    img = qrcode.make(url, border=2)
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _bot_send_photo(token: str, chat_id: str, png: bytes, caption: str) -> None:
    import httpx

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    async with httpx.AsyncClient(trust_env=True, timeout=120.0) as client:
        r = await client.post(
            url,
            data={"chat_id": chat_id, "caption": caption[:1024]},
            files={"photo": ("qr.png", png, "image/png")},
        )
        r.raise_for_status()
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(body)


async def _bot_send_message(token: str, chat_id: str, text: str) -> None:
    import httpx

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(trust_env=True, timeout=60.0) as client:
        r = await client.post(url, json={"chat_id": chat_id, "text": text[:4096]})
        r.raise_for_status()
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(body)


async def _run() -> int:
    if not _enabled():
        log.info("ENABLE_TELEGRAM_SIGNAL_AUTH is not 1 — skip Telethon preflight")
        return 0

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    token, chat_id = _bot_token(), _bot_chat_id()
    if not token or not chat_id:
        log.error("Need TELEGRAM_TOKEN and TELEGRAM_CHAT_ID (or FREQTRADE__TELEGRAM__*) for QR via bot")
        return 1

    try:
        api_id = int(os.environ["TELEGRAM_API_ID"])
        api_hash = os.environ["TELEGRAM_API_HASH"]
    except Exception:
        log.error("Set TELEGRAM_API_ID and TELEGRAM_API_HASH for Telethon")
        await _bot_send_message(
            token,
            chat_id,
            "Telethon preflight: TELEGRAM_API_ID / TELEGRAM_API_HASH missing in environment.",
        )
        return 1

    session_path = _session_path()
    parent = os.path.dirname(session_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    from telethon import TelegramClient
    from telethon import errors

    from freqtrade.signals.telethon_proxy import telethon_proxy_from_env

    proxy = telethon_proxy_from_env()
    if proxy:
        log.info("Telethon MTProto using proxy (via TELEGRAM_MTPROXY / TG_PROXY / HTTP_PROXY)")
    else:
        log.warning(
            "Telethon MTProto without proxy: direct connection to dc.telegram.org. "
            "If blocked or timing out, set TG_PROXY or TELEGRAM_MTPROXY (same URL as for bot)."
        )

    client = TelegramClient(session_path, api_id, api_hash, proxy=proxy)
    try:
        await client.connect()
    except (ConnectionError, OSError, TimeoutError, asyncio.TimeoutError) as e:
        log.exception("Telethon connect failed")
        try:
            await _bot_send_message(
                token,
                chat_id,
                "Telethon: no connection to Telegram servers (MTProto, timeout). "
                "Set MTProto proxy in .env (usually same as TG_PROXY, e.g. socks5://...), "
                "then rebuild/restart the container. "
                f"Details: {e!s}",
            )
        except Exception:
            pass
        try:
            await client.disconnect()
        except Exception:
            pass
        return 1

    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            handle = f"@{me.username}" if me.username else str(me.id)
            log.info("Telethon session already OK: %s", handle)
            await _bot_send_message(
                token,
                chat_id,
                f"✅ Telethon (channel): session active — {handle}",
            )
            return 0

        await _bot_send_message(
            token,
            chat_id,
            "🔐 Telethon: authorization required. In Telegram: Settings → Devices → "
            "Link Desktop Device — scan the following QR.",
        )

        while True:
            qr_login = await client.qr_login()
            png = _qr_png(qr_login.url)
            try:
                await _bot_send_photo(
                    token,
                    chat_id,
                    png,
                    "QR code for Telethon login (scan with Telegram app). Code will refresh if it expires.",
                )
            except Exception as e:
                log.exception("sendPhoto failed")
                await _bot_send_message(token, chat_id, f"Failed to send QR: {e!s}")
                return 1

            try:
                await qr_login.wait(timeout=120)
                break
            except asyncio.TimeoutError:
                await _bot_send_message(token, chat_id, "QR expired, sending a new one…")
            except errors.SessionPasswordNeededError:
                pwd = os.environ.get("TELEGRAM_2FA_PASSWORD", "").strip()
                if not pwd:
                    await _bot_send_message(
                        token,
                        chat_id,
                        "2FA Cloud Password required. Set TELEGRAM_2FA_PASSWORD in .env and restart the container.",
                    )
                    return 1
                await client.sign_in(password=pwd)
                break

        me = await client.get_me()
        handle = f"@{me.username}" if me.username else str(me.id)
        log.info("Telethon authorized: %s", handle)
        await _bot_send_message(
            token,
            chat_id,
            f"✅ Telethon authorized: {handle}. Session saved.",
        )
        return 0
    finally:
        await client.disconnect()


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
