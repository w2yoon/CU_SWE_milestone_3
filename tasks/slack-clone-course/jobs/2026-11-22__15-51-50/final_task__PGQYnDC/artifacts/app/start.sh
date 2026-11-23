#!/bin/sh
set -u
mkdir -p /app/data
[ -f /app/data/state.json ] || echo '{"users":{},"tokens":{},"workspaces":{},"channels":{},"messages":{},"files":{},"invites":{},"groups":{},"dms":{},"events":{},"seq":{}}' > /app/data/state.json
start_redis() {
  if command -v redis-server >/dev/null 2>&1 && ! (command -v redis-cli >/dev/null 2>&1 && redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null | grep -q PONG); then
    redis-server --daemonize yes --bind 127.0.0.1 --port 6379 >/dev/null 2>&1 || true
  fi
}
start_node() {
  port="$1"; pidfile="/app/node${port}.pid"
  python3 /app/app.py "$port" >/app/node${port}.log 2>&1 &
  echo $! > "$pidfile"
}
start_redis
for port in 8000 8001 8002; do start_node "$port"; done
trap 'exit 0' INT TERM
while :; do
  start_redis
  for port in 8000 8001 8002; do
    pidfile="/app/node${port}.pid"
    if [ ! -s "$pidfile" ] || ! kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null; then
      start_node "$port"
    fi
  done
  sleep 1
done
