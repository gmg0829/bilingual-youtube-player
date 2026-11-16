#!/bin/sh
# Start the bilingual YouTube player.
PORT="${1:-8765}"
cd "$(dirname "$0")" || exit 1
echo "▶ http://127.0.0.1:$PORT/"
exec python3 server.py --port "$PORT"
