#!/usr/bin/env python3
"""Debug: print :class:`SignalIngestEvent` rows from Telethon JSON (``dump_channel_messages_json.py``)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from freqtrade.signals.telethon_message import iter_ingest_events_from_telethon_json


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse Telethon message JSON into ingest events.")
    ap.add_argument("file", type=Path, help="Path to JSON (list or {messages: [...]})")
    ap.add_argument("--limit", type=int, default=0, help="Max events (0 = all)")
    ap.add_argument(
        "--default-channel-id",
        type=int,
        default=0,
        help="If set, use when peer_id is not PeerChannel (optional)",
    )
    args = ap.parse_args()
    if not args.file.is_file():
        print(f"Not a file: {args.file}", file=sys.stderr)
        return 1
    dc = args.default_channel_id or None
    n = 0
    for ev in iter_ingest_events_from_telethon_json(args.file, default_channel_id=dc):
        print(f"{ev.occurred_at.isoformat(sep=' ')} | {ev.idempotency_key} | {ev.text[:160]}")
        n += 1
        if args.limit and n >= args.limit:
            break
    print(f"# events: {n}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
