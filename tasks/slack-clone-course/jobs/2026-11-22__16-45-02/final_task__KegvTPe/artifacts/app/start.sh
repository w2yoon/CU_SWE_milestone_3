#!/bin/sh
mkdir -p /app
[ -f /app/data.json ] || echo '{"users":{},"tokens":{},"workspaces":{},"channels":{},"messages":{},"reactions":{},"files":{}}' > /app/data.json
python3 /app/server.py 8000 & echo $! >/app/node0.pid
python3 /app/server.py 8001 & echo $! >/app/node1.pid
python3 /app/server.py 8002 & echo $! >/app/node2.pid
wait
