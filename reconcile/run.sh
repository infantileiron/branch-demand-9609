#!/usr/bin/env bash
# 零依赖启动：仅需 Python 3.11 标准库
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$PWD"
export PORT="${PORT:-18309}"
exec python3 -m app.http_server
