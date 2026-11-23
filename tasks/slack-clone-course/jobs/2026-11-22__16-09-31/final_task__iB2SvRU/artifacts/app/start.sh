#!/bin/sh
set -eu
mkdir -p /app/data
PIDS=""
R=""
cleanup() {
  trap - INT TERM EXIT
  for p in $PIDS "$R"; do
    [ -n "${p:-}" ] && kill "$p" 2>/dev/null || true
  done
  exit 0
}
trap cleanup INT TERM EXIT
start_redis() {
  if ! kill -0 "$R" 2>/dev/null; then
    redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/app/data/redis.log 2>&1 &
    R=$!
  fi
}
start_node() {
  port="$1"
  if [ "$port" = "8000" ]; then
    python3 -u /app/server.py &
  else
    PORT="$port" python3 -u /app/server.py &
  fi
  eval "P$port=$!"
}
start_redis
start_node 8000
start_node 8001
start_node 8002
while :; do
  start_redis
  for port in 8000 8001 8002; do
    eval "pid=\${P$port:-}"
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
      start_node "$port"
    fi
  done
  sleep 1
done
