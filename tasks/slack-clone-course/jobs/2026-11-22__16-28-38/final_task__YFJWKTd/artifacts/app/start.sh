#!/bin/sh
mkdir -p /app/data
start_redis() {
  if ! ss -ltn 2>/dev/null | grep -q ':6379 '; then
    redis-server --bind 127.0.0.1 --port 6379 --daemonize yes --save '' --appendonly no >/tmp/huddle-redis.log 2>&1 || true
  fi
}
start_node() {
  port="$1"; pidfile="/tmp/huddle${port}.pid"
  if [ ! -f "$pidfile" ] || ! kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null; then
    if [ "$port" = 8000 ]; then
      python3 /app/app.py >/tmp/huddle-8000.log 2>&1 &
    else
      PORT="$port" python3 /app/app.py >/tmp/huddle-"$port".log 2>&1 &
    fi
    echo $! >"$pidfile"
  fi
}
start_redis
start_node 8000
start_node 8001
start_node 8002
trap 'exit 0' INT TERM
while :; do
  start_redis
  start_node 8000
  start_node 8001
  start_node 8002
  sleep 1
done
