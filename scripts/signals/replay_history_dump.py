#!/usr/bin/env python3
"""Debug: print parsed ingest events from a history export file."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running before/without full freqtrade install when cwd is repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from freqtrade.signals.history_export import iter_history_export_events


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse channel history export (C.replay.1).")
    ap.add_argument("file", type=Path, help="Path to history_*.txt")
    ap.add_argument("--limit", type=int, default=0, help="Max events (0 = all)")
    ap.add_argument("--encoding", default="utf-8")
    args = ap.parse_args()
    if not args.file.is_file():
        print(f"Not a file: {args.file}", file=sys.stderr)
        return 1
    n = 0
    for ev in iter_history_export_events(args.file, encoding=args.encoding):
        print(f"{ev.occurred_at.isoformat(sep=' ')} | {ev.idempotency_key} | {ev.text[:120]}")
        n += 1
        if args.limit and n >= args.limit:
            break
    print(f"# events: {n}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
