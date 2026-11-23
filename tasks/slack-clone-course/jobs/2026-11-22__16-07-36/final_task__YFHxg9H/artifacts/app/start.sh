#!/bin/sh
set -eu
pids=""
cleanup() {
  trap - TERM INT EXIT
  for p in $pids; do kill "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup TERM INT EXIT
(
  while :; do
    redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/huddle-redis.log 2>&1 || true
    sleep 1
  done
) &
pids="$pids $!"
python3 /app/app.py --irc >/tmp/huddle-irc.log 2>&1 &
pids="$pids $!"
for port in 8000 8001 8002; do
  (
    while :; do
      python3 /app/app.py --http "$port" >/tmp/huddle-$port.log 2>&1 || true
      sleep 1
    done
  ) &
  pids="$pids $!"
done
while :; do
  sleep 3600
done
