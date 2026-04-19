#!/usr/bin/env python3
"""
Fetch recent channel messages via Telethon and write JSON (``Message.to_dict()`` each).

Environment:
  TELEGRAM_API_ID, TELEGRAM_API_HASH — https://my.telegram.org
  TELEGRAM_SIGNALS_CHANNEL_ID — numeric channel id (e.g. 1566432615; peer is -100<id>)
  TELEGRAM_SESSION_PATH — optional, default user_data/.secrets/telegram_signals.session
  TG_PROXY / HTTP_PROXY / … — see telethon_proxy.py

Usage:
  cd <repo> && export TELEGRAM_API_ID=... TELEGRAM_API_HASH=... TELEGRAM_SIGNALS_CHANNEL_ID=...
  python scripts/signals/dump_channel_messages_json.py --limit 20 --out /tmp/channel.json

Refresh tests/fixtures/signals_channel_messages.json after sampling (trim if needed).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SIGNALS_DIR = Path(__file__).resolve().parent
for _p in (_REPO_ROOT, _SIGNALS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _session_path() -> str:
    default = os.path.join("user_data", ".secrets", "telegram_signals.session")
    return os.path.abspath(os.environ.get("TELEGRAM_SESSION_PATH", default))


async def _run() -> int:
    ap = argparse.ArgumentParser(description="Dump Telethon Message.to_dict() JSON from a channel.")
    ap.add_argument("--limit", type=int, default=20, help="Max messages (newest first)")
    ap.add_argument("--out", type=Path, required=True, help="Output JSON path")
    ap.add_argument(
        "--wrap",
        action="store_true",
        help="Write {\"channel_id\": N, \"messages\": [...]} instead of a bare list",
    )
    args = ap.parse_args()

    try:
        api_id = int(os.environ["TELEGRAM_API_ID"])
        api_hash = os.environ["TELEGRAM_API_HASH"]
        ch_raw = os.environ["TELEGRAM_SIGNALS_CHANNEL_ID"].strip()
    except KeyError as e:
        print(f"Missing env: {e.args[0]}", file=sys.stderr)
        return 1

    ch_id = int(ch_raw)
    peer_id = ch_id if str(ch_raw).startswith("-") else int(f"-100{ch_id}")

    from telethon import TelegramClient
    from telethon_proxy import telethon_proxy_from_env

    proxy = telethon_proxy_from_env()
    client = TelegramClient(_session_path(), api_id, api_hash, proxy=proxy)
    await client.connect()
    if not await client.is_user_authorized():
        print("Session not authorized — run preflight / telegram_qr_login.py", file=sys.stderr)
        await client.disconnect()
        return 1

    entity = await client.get_entity(peer_id)
    rows: list[dict] = []
    async for msg in client.iter_messages(entity, limit=args.limit):
        rows.append(msg.to_dict())

    await client.disconnect()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.wrap:
        payload = {"channel_id": ch_id, "message_count": len(rows), "messages": rows}
    else:
        payload = rows
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default), encoding="utf-8")
    print(f"Wrote {len(rows)} messages to {args.out}", file=sys.stderr)
    return 0


def _json_default(obj: object) -> object:
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"not JSON serializable: {type(obj).__name__}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
