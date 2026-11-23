#!/bin/sh
set -eu
mkdir -p /app/data
for f in /app/node0.pid /app/node1.pid /app/node2.pid /app/redis.pid; do
  if [ -f "$f" ]; then kill "$(cat "$f")" 2>/dev/null || true; fi
done
sleep 1
if command -v redis-server >/dev/null 2>&1; then
  redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/huddle-redis.log 2>&1 &
  echo $! >/app/redis.pid
fi
for n in 0 1 2; do
  NODE=$n python3 /app/app.py $((8000+n)) &
  echo $! > /app/node$n.pid
done
python3 - <<'PY'
import socket,threading
# lightweight IRC gateway
def run(c):
 nick='guest'; buf=b''
 while 1:
  d=c.recv(4096)
  if not d:break
  buf+=d
  while b'\r\n' in buf:
   line,buf=buf.split(b'\r\n',1); a=line.decode(errors='ignore').split(); cmd=a[0].upper() if a else ''
   if cmd=='NICK' and len(a)>1:nick=a[1]
   elif cmd=='PING':c.sendall(b':huddle PONG huddle\r\n')
   elif cmd=='USER':c.sendall((f':huddle 001 {nick} :Welcome\r\n:huddle 002 {nick} :Huddle\r\n:huddle 003 {nick} :2026\r\n:huddle 004 {nick} huddle\r\n:huddle 005 {nick} CHANTYPES=# :are supported\r\n:huddle 422 {nick} :No MOTD\r\n').encode())
   elif cmd=='QUIT':return
 s.close()
s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen()
while 1:
 c,_=s.accept();threading.Thread(target=run,args=(c,),daemon=True).start()
PY
