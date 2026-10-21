#!/bin/zsh
# Double-click me: starts the Campus Recycling Helper and opens your browser.
# Stop the server later with:  lsof -ti :8010 | xargs kill

PORT=8010
DIR="${0:A:h}"
PY="$HOME/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
[[ -x "$PY" ]] || PY="python3"

if lsof -ti :$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Server already running on port $PORT."
else
  echo "Starting Campus Recycling Helper on http://127.0.0.1:$PORT ..."
  ( cd "$DIR" && nohup "$PY" server.py --port $PORT > /tmp/crh_server.log 2>&1 & )
  sleep 2
fi

open "http://127.0.0.1:$PORT"
