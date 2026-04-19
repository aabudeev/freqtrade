#!/usr/bin/env python3
"""
Create or refresh a Telethon session using QR login (scan with Telegram app).

Environment:
  TELEGRAM_API_ID     — int, from https://my.telegram.org
  TELEGRAM_API_HASH   — string
  TELEGRAM_SESSION_PATH — optional, default user_data/.secrets/telegram_signals.session
  TELEGRAM_2FA_PASSWORD — optional; else prompted if account has 2FA

Usage:
  pip install -r requirements-signals.txt
  export TELEGRAM_API_ID=... TELEGRAM_API_HASH=...
  python scripts/signals/telegram_qr_login.py

See docs/private/phase-c-signals-architecture.md
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys


def _print_qr(url: str) -> None:
    try:
        import qrcode
    except ImportError:
        print("Install qrcode: pip install qrcode", file=sys.stderr)
        print(url, file=sys.stderr)
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    tty = sys.stdout.isatty()
    qr.print_ascii(invert=True, tty=tty)


async def _qr_loop(client) -> None:
    from telethon import errors

    while True:
        qr_login = await client.qr_login()
        print("Scan the QR code with Telegram (Settings → Devices → Link).", file=sys.stderr)
        _print_qr(qr_login.url)
        try:
            await qr_login.wait(timeout=120)
            return
        except asyncio.TimeoutError:
            print("QR expired, generating a new one…", file=sys.stderr)
        except errors.SessionPasswordNeededError:
            pwd = os.environ.get("TELEGRAM_2FA_PASSWORD")
            if not pwd:
                pwd = getpass.getpass("Two-factor password: ")
            await client.sign_in(password=pwd)
            return


async def main() -> int:
    parser = argparse.ArgumentParser(description="Telethon QR login for signal ingest session.")
    parser.add_argument(
        "--session",
        default=os.environ.get(
            "TELEGRAM_SESSION_PATH",
            os.path.join("user_data", ".secrets", "telegram_signals.session"),
        ),
        help="Path to Telethon session file (default: user_data/.secrets/telegram_signals.session)",
    )
    args = parser.parse_args()

    try:
        api_id = int(os.environ["TELEGRAM_API_ID"])
        api_hash = os.environ["TELEGRAM_API_HASH"]
    except KeyError as e:
        print("Set TELEGRAM_API_ID and TELEGRAM_API_HASH.", file=sys.stderr)
        print(f"Missing: {e.args[0]}", file=sys.stderr)
        return 1

    session_path = os.path.abspath(args.session)
    parent = os.path.dirname(session_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    from telethon import TelegramClient

    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Already authorized as @{me.username or me.id} — session: {session_path}")
        await client.disconnect()
        return 0

    try:
        await _qr_loop(client)
    except KeyboardInterrupt:
        print("Aborted.", file=sys.stderr)
        await client.disconnect()
        return 130

    me = await client.get_me()
    print(f"Authorized as @{me.username or me.id} — session: {session_path}")
    await client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
