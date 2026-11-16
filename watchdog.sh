#!/bin/sh
# Keeps the player service alive: restarts it whenever it dies
# (the phone can squeeze it out when memory is tight).
PORT="${1:-8765}"
DIR="$(dirname "$0")"
cd "$DIR" || exit 1
while :; do
  if ! curl -s --max-time 5 -o /dev/null "http://127.0.0.1:$PORT/api/health" 2>/dev/null; then
    echo "[watchdog] $(date '+%H:%M:%S') 服务不在了，重新拉起" >> /tmp/ytb.log
    python3 server.py --port "$PORT" >> /tmp/ytb.log 2>&1
  fi
  sleep 5
done
