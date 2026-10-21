#!/bin/zsh
# Double-click me: starts the Campus Recycling Helper and opens your browser.
# Always restarts the server first, so the browser never talks to a stale
# process serving old code. Stop the server with:  lsof -ti :8010 | xargs kill

PORT=8010
DIR="${0:A:h}"
PY="$HOME/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
[[ -x "$PY" ]] || PY="python3"

OLD=$(lsof -ti :$PORT -sTCP:LISTEN 2>/dev/null)
if [[ -n "$OLD" ]]; then
  echo "Stopping stale server (pid $OLD) - it may be serving outdated code."
  kill $OLD 2>/dev/null
  sleep 1
fi

echo "Starting Campus Recycling Helper on http://127.0.0.1:$PORT ..."
( cd "$DIR" && nohup "$PY" server.py --port $PORT > /tmp/crh_server.log 2>&1 & )
sleep 2

VERSION=$(git -C "$DIR" rev-parse --short HEAD 2>/dev/null)
[[ -n "$VERSION" ]] && echo "Server running commit $VERSION - see /tmp/crh_server.log"

open "http://127.0.0.1:$PORT"
