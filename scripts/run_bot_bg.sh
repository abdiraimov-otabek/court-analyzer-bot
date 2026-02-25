#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate

# stop previous bot if running
pkill -TERM -f "python.*src.app.run_bot" 2>/dev/null || true
sleep 1

mkdir -p logs
nohup python3 -m src.app.run_bot > logs/telegram-bot.out 2>&1 &
echo $! > logs/telegram-bot.pid

echo "started bot pid $(cat logs/telegram-bot.pid)"
