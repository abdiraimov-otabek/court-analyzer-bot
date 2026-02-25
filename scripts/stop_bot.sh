#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f logs/telegram-bot.pid ]; then
  pid=$(cat logs/telegram-bot.pid)
  kill -TERM "$pid" 2>/dev/null || true
  rm -f logs/telegram-bot.pid
fi
# fallback
pkill -TERM -f "python.*src.app.run_bot" 2>/dev/null || true

echo "stopped"
