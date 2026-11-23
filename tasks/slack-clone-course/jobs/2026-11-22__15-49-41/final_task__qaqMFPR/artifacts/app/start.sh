#!/bin/sh
set -eu
python3 - <<'PY'
import sqlite3
sqlite3.connect('/app/huddle.db').close()
PY
python3 /app/server.py >/tmp/huddle-8000.log 2>&1 & echo $! >/tmp/huddle-8000.pid
PORT=8001 python3 /app/server.py >/tmp/huddle-8001.log 2>&1 & echo $! >/tmp/huddle-8001.pid
PORT=8002 python3 /app/server.py >/tmp/huddle-8002.log 2>&1 & echo $! >/tmp/huddle-8002.pid
wait
