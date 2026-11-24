#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
umask 077
set -a
source .env
set +a
exec .venv/bin/python -u bot.py "$@"
