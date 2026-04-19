#!/usr/bin/env bash
# shellcheck disable=SC2086
set -euo pipefail
python /freqtrade/fork/scripts/signals/preflight_telegram_auth.py
python /freqtrade/fork/scripts/signals/preflight_channel_smoke.py
exec freqtrade "$@"
