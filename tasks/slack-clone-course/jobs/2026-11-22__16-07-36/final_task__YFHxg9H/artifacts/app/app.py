import os, re, json, uuid, sqlite3, hashlib, secrets, threading, time, mimetypes, base64, struct, socket
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DB='/app/data/huddle.db'; os.makedirs('/app/data',exist_ok=True)
db=sqlite3.connect(DB,check_same_thread=False); db.row_factory=sqlite3.Row
lock=threading.RLock(); listeners={}; irc_clients=[]
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def init():
 with lock:
  db.executescript('''
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
  CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
  CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
  CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
  CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
  CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
  CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,UNIQUE(message_id,user_id,emoji));
  CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
  CREATE TABLE IF NOT EXISTS dms(user1 INTEGER,user2 INTEGER,channel_id INTEGER,PRIMARY KEY(user1,user2));
  CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
  CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
  CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT);
  CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,expires INTEGER,max_uses INTEGER,uses INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS readstate(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
  CREATE TABLE IF NOT EXISTS channel_sequences(channel_id INTEGER PRIMARY KEY,event_id INTEGER DEFAULT 0);
  ''');
  db.execute('INSERT OR IGNORE INTO channel_sequences(channel_id,event_id) SELECT channel_id,COALESCE(MAX(event_id),0) FROM messages GROUP BY channel_id');
  db.execute('UPDATE channel_sequences SET event_id=CASE WHEN event_id < COALESCE((SELECT MAX(m.event_id) FROM messages m WHERE m.channel_id=channel_sequences.channel_id),0) THEN COALESCE((SELECT MAX(m.event_id) FROM messages m WHERE m.channel_id=channel_sequences.channel_id),0) ELSE event_id END');
  db.commit()
init()
def q(sql,args=(),one=False):
 with lock:
  c=db.execute(sql,args); r=c.fetchone() if one else c.fetchall(); db.commit(); return r
def userobj(r):
 return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def chan(r): return {k:r[k] for k in ('id','workspace_id','name','is_private','is_dm','topic','is_archived')}
def msg(r):
 if not r:return None
 reactions=[]
 for x in q('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)):
  reactions.append({'emoji':x['emoji'],'count':x['n'],'user_ids':[int(a) for a in x['ids'].split(',')]})
 au=q('SELECT * FROM users WHERE id=?',(r['author_id'],),True)
 ch=q('SELECT workspace_id FROM channels WHERE id=?',(r['channel_id'],),True)
 mentions=[]
 if ch:
  names=re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_]{2,32})',r['body'] or '')
  for name in names:
   z=q('SELECT u.id FROM users u JOIN members m ON m.user_id=u.id WHERE m.workspace_id=? AND lower(u.username)=lower(?) AND u.id<>?',(ch['workspace_id'],name,r['author_id']),True)
   if z and z['id'] not in mentions: mentions.append(z['id'])
 fs=[dict(f) for f in q('SELECT f.id,f.uploader_id,f.filename,f.content_type,f.size,f.created_at FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(r['id'],))]
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(au),'body':r['body'],'parent_id':r['parent_id'],'reply_count':q('SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],),True)['n'],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':fs,'reactions':reactions,'mentions':mentions}
def event(cid,kind,payload):
 cid=int(cid)
 with lock:
  try:
   db.execute('BEGIN IMMEDIATE')
   row=db.execute('SELECT event_id FROM channel_sequences WHERE channel_id=?',(cid,)).fetchone()
   eid=(int(row[0]) if row else 0)+1
   if row: db.execute('UPDATE channel_sequences SET event_id=? WHERE channel_id=?',(eid,cid))
   else: db.execute('INSERT INTO channel_sequences(channel_id,event_id) VALUES(?,?)',(cid,eid))
   db.commit()
  except Exception:
   db.rollback()
   row=q('SELECT COALESCE(MAX(event_id),0)+1 n FROM messages WHERE channel_id=?',(cid,),True)
   eid=row['n']
  if kind in ('message.created','message.reply') and payload.get('message'):
   ch=q('SELECT c.name,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(cid,),True)
   if ch:
    m=payload['message']; author=m.get('author',{}).get('username','user'); text=m.get('body','')
    for ic in list(irc_clients):
     if cid in getattr(ic,'channels',set()):
      try: ic.send(':'+author+'!user@huddle PRIVMSG #'+ch['slug']+'/'+ch['name']+' :'+text)
      except Exception:
       try: irc_clients.remove(ic)
       except ValueError: pass
  return eid
def jsonbody(h):
 n=int(h.headers.get('Content-Length','0')); return json.loads(h.rfile.read(n) or b'{}')
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def send(self,status,obj=None,ctype='application/json',extra=None):
  b=(obj if isinstance(obj,(bytes,bytearray)) else json.dumps(obj).encode() if obj is not None else b'')
  self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(b)))
  if extra:
   for k,v in extra.items():self.send_header(k,v)
  self.end_headers()
  if b:self.wfile.write(b)
 def auth(self):
  t=self.headers.get('Authorization','').replace('Bearer ','')
  r=q('SELECT user_id FROM tokens WHERE token=?',(t,),True)
  return r['user_id'] if r else None
 def reqpath(self): return urlparse(self.path).path
 def do_GET(self):
  p=self.reqpath(); u=self.auth()
  if p=='/api/ws' and self.headers.get('Upgrade','').lower()=='websocket':
   tok=parse_qs(urlparse(self.path).query).get('token',[''])[0].replace('Bearer ','')
   if not q('SELECT user_id FROM tokens WHERE token=?',(tok,),True):
    return self.send(401,{'error':'unauthorized'})
   key=self.headers.get('Sec-WebSocket-Key','')
   import hashlib as _hh, base64 as _bb
   accept=_bb.b64encode(_hh.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
   self.send_response(101); self.send_header('Upgrade','WebSocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers()
   ws_loop(self.connection)
   return
  if p=='/': return self.send(200,HTML,'text/html')
  if p=='/api/health': return self.send(200,{'status':'ok','node_id':int(self.server.server_port)-8000})
  if p=='/api/auth/me':
   if not u:return self.send(401,{'error':'unauthorized'})
   return self.send(200,{'user':userobj(q('SELECT * FROM users WHERE id=?',(u,),True))})
  if p.startswith('/api/users/'):
   r=q('SELECT * FROM users WHERE id=?',(p.split('/')[-1],),True); return self.send(200 if r else 404,{'user':userobj(r)} if r else {'error':'not found'})
  if not u:return self.send(401,{'error':'unauthorized'})
  if p=='/api/workspaces':
   rows=q('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u,))
   return self.send(200,{'workspaces':[dict(x) for x in rows]})
  if p.startswith('/api/workspaces/'):
   slug=p.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.send(404,{'error':'not found'})
   if p.endswith('/members'):
    rows=q('SELECT m.*,u.username,u.display_name FROM members m JOIN users u ON u.id=m.user_id WHERE workspace_id=?',(w['id'],))
    return self.send(200,{'members':[dict(x) for x in rows]})
   if p.endswith('/groups'):
    rows=q('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],)); return self.send(200,{'groups':[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])} for x in rows]})
   cs=q('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR is_archived IS NULL)',(w['id'],))
   return self.send(200,{'workspace':dict(w),'channels':[chan(x) for x in cs],'read_state':[]})
  if p.startswith('/api/channels/') and p.endswith('/read'):
   cid=p.split('/')[3]
   if not q('SELECT id FROM channels WHERE id=?',(cid,),True): return self.send(404,{'error':'not found'})
   r=q('SELECT COALESCE(last_read_event_id,0) n FROM readstate WHERE user_id=? AND channel_id=?',(u,cid),True); val=r['n'] if r else 0
   return self.send(200,{'read_state':{'channel_id':int(cid),'last_read_event_id':val,'unread_count':0,'mention_count':0}})
  if p.startswith('/api/channels/') and p.endswith('/messages'):
   cid=p.split('/')[3]
   if not q('SELECT id FROM channels WHERE id=?',(cid,),True): return self.send(404,{'error':'not found'})
   qs=parse_qs(urlparse(self.path).query)
   try: lim=int(qs.get('limit',['50'])[0])
   except: return self.send(400,{'error':'invalid limit'})
   if lim<1 or lim>200:return self.send(400,{'error':'invalid limit'})
   cur=qs.get('cursor',[None])[0]
   if cur is not None and not cur.isdigit():return self.send(400,{'error':'invalid cursor'})
   args=[cid]; extra=''
   if cur is not None: extra=' AND id<?'; args.append(int(cur))
   rows=q('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0'+extra+' ORDER BY id DESC LIMIT '+str(lim),tuple(args))
   nxt=str(rows[-1]['id']) if len(rows)==lim else None
   return self.send(200,{'messages':[msg(x) for x in rows],'next_cursor':nxt})
  if p.startswith('/api/messages/') and p.endswith('/replies'):
   mid=p.split('/')[3]; rows=q('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(mid,))
   return self.send(200,{'replies':[msg(x) for x in rows],'next_cursor':None})
  if p.startswith('/api/channels/') and p.endswith('/replies'):
   mid=p.split('/')[3]; rows=q('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(mid,))
   return self.send(200,{'replies':[msg(x) for x in rows],'next_cursor':None})
  if p.startswith('/api/channels/') and p.endswith('/members'):
   cid=p.split('/')[3]; rows=q('SELECT u.* FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(cid,)); return self.send(200,{'members':[userobj(x) for x in rows]})
  if p.startswith('/api/channels/') and p.endswith('/pins'):
   cid=p.split('/')[3]
   if not q('SELECT id FROM channels WHERE id=?',(cid,),True): return self.send(404,{'error':'not found'})
   rows=q('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=?',(cid,)); return self.send(200,{'pins':[{'message':msg(x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rows],'next_cursor':None})
  if p=='/api/search':
   term=parse_qs(urlparse(self.path).query).get('q',[''])[0]; rows=q('SELECT * FROM messages WHERE body LIKE ? AND deleted=0 ORDER BY id DESC',(f'%{term}%',)); return self.send(200,{'results':[msg(x) for x in rows],'next_cursor':None})
  if p.startswith('/api/files/') and p.endswith('/download'):
   r=q('SELECT * FROM files WHERE id=?',(p.split('/')[3],),True)
   if not r:return self.send(404,{'error':'not found'})
   return self.send(200,r['data'],r['content_type'],{'Content-Disposition':f'attachment; filename="{r["filename"]}"'})
  if p.startswith('/api/files/'):
   r=q('SELECT * FROM files WHERE id=?',(p.split('/')[-1],),True); return self.send(200,{'file':dict(r)}) if r else self.send(404,{'error':'not found'})
  return self.send(404,{'error':'not found'})
 def do_POST(self):
  p=self.reqpath(); 
  if p=='/api/auth/register':
   d=jsonbody(self); un=d.get('username','')
   if not re.fullmatch(r'[A-Za-z0-9_]{2,32}',un) or len(d.get('password',''))<8:return self.send(400,{'error':'invalid registration'})
   try:q('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,hashlib.sha256(d['password'].encode()).hexdigest(),d.get('display_name') or un))
   except sqlite3.IntegrityError:return self.send(409,{'error':'duplicate'})
   uid=q('SELECT id FROM users WHERE username=?',(un,),True)['id']; t=secrets.token_urlsafe(24);q('INSERT INTO tokens VALUES(?,?)',(t,uid))
   return self.send(201,{'user':userobj(q('SELECT * FROM users WHERE id=?',(uid,),True)),'token':t})
  if p=='/api/auth/login':
   d=jsonbody(self); r=q('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),hashlib.sha256(d.get('password','').encode()).hexdigest()),True)
   if not r:return self.send(401,{'error':'invalid credentials'})
   t=secrets.token_urlsafe(24);q('INSERT INTO tokens VALUES(?,?)',(t,r['id'],));return self.send(200,{'user':userobj(r),'token':t})
  u=self.auth()
  if not u:return self.send(401,{'error':'unauthorized'})
  if p=='/api/files':
   n=int(self.headers.get('Content-Length','0'))
   if n>10*1024*1024:return self.send(413,{'error':'file too large'})
   raw=self.rfile.read(n); ct=self.headers.get('Content-Type','')
   m=re.search(r'boundary=([^;]+)',ct)
   if not m:return self.send(400,{'error':'multipart required'})
   boundary=('--'+m.group(1).strip('"')).encode(); part=raw.split(boundary)
   filename='upload.bin'; data=b''
   for z in part:
    if b'Content-Disposition' in z and b'filename=' in z:
     h,_,data=z.partition(b'\r\n\r\n'); fm=re.search(br'filename="([^"]*)"',h); filename=(fm.group(1).decode() if fm else filename); data=data.rstrip(b'\r\n-')
   fcm=re.search(br'(?im)^Content-Type:\s*([^\r\n]+)',h) if 'h' in locals() else None
   file_ct=(fcm.group(1).decode().strip() if fcm else (mimetypes.guess_type(filename)[0] or 'application/octet-stream'))
   q('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u,filename,file_ct,len(data),now(),data))
   r=q('SELECT * FROM files WHERE id=last_insert_rowid()',(),True); return self.send(201,{'file':{k:r[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
  if p.startswith('/api/invitations/') and p.endswith('/accept'):
   code=p.split('/')[3]; inv=q('SELECT * FROM invites WHERE code=?',(code,),True)
   if not inv or (inv['invited_user_id'] and inv['invited_user_id']!=u) or (inv['expires'] and inv['expires']<int(time.time())) or inv['uses']>=inv['max_uses']:return self.send(404 if not inv or not (inv['invited_user_id'] and inv['invited_user_id']!=u) else 403,{'error':'invalid invitation'})
   q('INSERT OR IGNORE INTO members VALUES(?,?,?)',(inv['workspace_id'],u,'member')); q('UPDATE invites SET uses=uses+1 WHERE code=?',(code,)); return self.send(200,{'accepted':True})
  if p.startswith('/api/workspaces/') and p.endswith('/transfer_ownership'):
   slug=p.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.send(404,{'error':'not found'})
   if w['owner_id']!=u:return self.send(403,{'error':'forbidden'})
   d=jsonbody(self); target=d.get('new_owner_id')
   m=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target),True)
   if not m or m['role']!='admin':return self.send(400,{'error':'target must be admin'})
   q('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u))
   q('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],target))
   q('UPDATE workspaces SET owner_id=? WHERE id=?',(target,w['id']))
   return self.send(200,{'workspace':dict(q('SELECT * FROM workspaces WHERE id=?',(w['id'],),True))})
  if p.startswith('/api/workspaces/') and p.endswith('/groups'):
   slug=p.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.send(404,{'error':'not found'})
   d=jsonbody(self); handle=d.get('handle','').lstrip('@'); name=d.get('name',handle); ids=d.get('user_ids',[])
   q('INSERT INTO groups_(workspace_id,handle,name,creator_id,user_ids) VALUES(?,?,?,?,?)',(w['id'],handle,name,u,json.dumps(ids)))
   r=q('SELECT * FROM groups_ WHERE id=last_insert_rowid()',(),True); return self.send(201,{'group':{'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':ids}})
  if p.startswith('/api/workspaces/') and p.endswith('/invitations'):
   slug=p.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.send(404,{'error':'not found'})
   d=jsonbody(self); code=secrets.token_urlsafe(12); exp=int(time.time())+int(d.get('expires_in_seconds',604800)); maxu=int(d.get('max_uses',1)); target=d.get('invited_user_id')
   q('INSERT INTO invites(code,workspace_id,invited_user_id,expires,max_uses) VALUES(?,?,?,?,?)',(code,w['id'],target,exp,maxu)); return self.send(201,{'invitation':{'code':code,'workspace_id':w['id'],'expires_at':exp,'max_uses':maxu,'uses':0}})
  if p=='/api/workspaces':
   d=jsonbody(self)
   if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return self.send(400,{'error':'invalid slug'})
   try:q('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(d['slug'],d.get('name',d['slug']),u))
   except sqlite3.IntegrityError:return self.send(409,{'error':'duplicate'})
   w=q('SELECT * FROM workspaces WHERE slug=?',(d['slug'],),True);q('INSERT INTO members VALUES(?,?,?)',(w['id'],u,'owner'));q('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(w['id'],'general')); c=q('SELECT * FROM channels WHERE workspace_id=?',(w['id'],),True);q('INSERT INTO cmembers VALUES(?,?)',(c['id'],u))
   return self.send(201,{'workspace':dict(w),'general_channel':chan(c)})
  if p.startswith('/api/workspaces/') and p.endswith('/channels'):
   slug=p.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True); d=jsonbody(self)
   if not w:return self.send(404,{'error':'not found'})
   me=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u),True)
   if not me:return self.send(403,{'error':'not a member'})
   if me['role']=='guest' or (d.get('is_private') and me['role'] not in ('owner','admin')):return self.send(403,{'error':'forbidden'})
   if q('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],d.get('name')),True):return self.send(409,{'error':'duplicate'})
   q('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d.get('name'),int(bool(d.get('is_private'))),d.get('topic',''))); c=q('SELECT * FROM channels WHERE id=last_insert_rowid()',(),True);q('INSERT INTO cmembers VALUES(?,?)',(c['id'],u));return self.send(201,{'channel':chan(c)})
  if p.startswith('/api/channels/') and (p.endswith('/archive') or p.endswith('/unarchive')):
   cid=p.split('/')[3]; c=q('SELECT * FROM channels WHERE id=?',(cid,),True)
   if not c:return self.send(404,{'error':'not found'})
   w=q('SELECT owner_id FROM workspaces WHERE id=?',(c['workspace_id'],),True)
   if not w or w['owner_id']!=u:return self.send(403,{'error':'forbidden'})
   archived=1 if p.endswith('/archive') else 0
   q('UPDATE channels SET is_archived=? WHERE id=?',(archived,cid))
   c=q('SELECT * FROM channels WHERE id=?',(cid,),True)
   event(cid,'channel.archived' if archived else 'channel.unarchived',{'channel':chan(c)})
   return self.send(200,{'channel':chan(c)})
  if p.startswith('/api/channels/') and p.endswith('/leave'):
   cid=p.split('/')[3]; c=q('SELECT * FROM channels WHERE id=?',(cid,),True)
   if not c:return self.send(404,{'error':'not found'})
   q('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u))
   return self.send(200,{'channel':chan(c)})
  if p.startswith('/api/channels/') and p.endswith('/join'):
   try:
    cid=int(p.split('/')[3]); c=q('SELECT * FROM channels WHERE id=?',(cid,),True)
    if not c:return self.send(404,{'error':'not found'})
    wid=int(c['workspace_id']); w=q('SELECT * FROM workspaces WHERE id=?',(wid,),True)
    if not w:return self.send(404,{'error':'not found'})
    mem=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(wid,u),True)
    joined=q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u),True)
    if c['is_private'] and not joined:return self.send(403,{'error':'forbidden'})
    if mem and mem['role']=='guest':return self.send(403,{'error':'forbidden'})
    if not mem and w['join_mode']=='invite_only':return self.send(403,{'error':'invite required'})
    q('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,u))
    q('INSERT OR IGNORE INTO members VALUES(?,?,?)',(wid,u,'member'))
    return self.send(200,{'channel':chan(c)})
   except Exception as e:
    return self.send(500,{'error':'join failed'})
  if p.startswith('/api/channels/') and p.endswith('/read'):
   cid=p.split('/')[3]
   if not q('SELECT id FROM channels WHERE id=?',(cid,),True):return self.send(404,{'error':'not found'})
   if self.command == 'POST':
    d=jsonbody(self); old=q('SELECT last_read_event_id FROM readstate WHERE user_id=? AND channel_id=?',(u,cid),True)
    val=max(int(d.get('last_read_event_id',0)), int(old['last_read_event_id']) if old else 0)
    q('INSERT OR REPLACE INTO readstate VALUES(?,?,?)',(u,cid,val))
   r=q('SELECT COALESCE(last_read_event_id,0) n FROM readstate WHERE user_id=? AND channel_id=?',(u,cid),True)
   val=r['n'] if r else 0
   return self.send(200,{'read_state':{'channel_id':int(cid),'last_read_event_id':val,'unread_count':0,'mention_count':0}})
  if p.startswith('/api/channels/') and p.endswith('/messages'):
   cid=p.split('/')[3]; d=jsonbody(self); body=d.get('body','')
   if body.startswith('/') and not body.startswith('//'):
    parts=body.split(' ',1); cmd=parts[0].lower(); arg=parts[1] if len(parts)>1 else ''
    if cmd=='/shrug': body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
    elif cmd=='/me':
     if not arg.strip():return self.send(400,{'error':'/me requires text'})
    elif cmd in ('/topic','/invite','/archive','/unarchive'):
     return self.send(400,{'error':'unsupported command'})
    else:return self.send(400,{'error':'unknown command'})
   if not body.strip():return self.send(400,{'error':'empty message'})
   c=q('SELECT * FROM channels WHERE id=?',(cid,),True)
   if not c:return self.send(404,{'error':'not found'})
   if c['is_archived']:return self.send(423,{'error':'archived'})
   files=d.get('file_ids') or []
   if not isinstance(files,list):return self.send(400,{'error':'invalid file_ids'})
   for fid in files:
    f=q('SELECT * FROM files WHERE id=?',(fid,),True)
    if not f or f['uploader_id']!=u or q('SELECT 1 FROM message_files WHERE file_id=?',(fid,),True):return self.send(400,{'error':'invalid attachment'})
   t=now();q('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u,body,d.get('parent_id'),t)); r=q('SELECT * FROM messages WHERE id=last_insert_rowid()',(),True)
   for fid in files:q('INSERT INTO message_files(message_id,file_id) VALUES(?,?)',(r['id'],fid))
   eid=event(cid,'message.reply' if d.get('parent_id') else 'message.created',{'message':msg(r)});q('UPDATE messages SET event_id=? WHERE id=?',(eid,r['id'])); return self.send(201,{'message':msg(r)})
  if p=='/api/dms':
   d=jsonbody(self); a,b=sorted((u,int(d['user_id']))); r=q('SELECT channel_id FROM dms WHERE user1=? AND user2=?',(a,b),True)
   if r:return self.send(200,{'channel_id':r['channel_id']})
   q('INSERT INTO channels(name,is_dm) VALUES(?,1)',(f'dm-{a}-{b}',)); cid=q('SELECT id FROM channels WHERE id=last_insert_rowid()',(),True)['id'];q('INSERT INTO dms VALUES(?,?,?)',(a,b,cid));q('INSERT OR IGNORE INTO cmembers VALUES(?,?),(?,?)',(cid,a,cid,b));return self.send(200,{'channel_id':cid})
  if p.startswith('/api/messages/') and p.endswith('/pin'):
   mid=p.split('/')[3]; ts=now(); q('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(mid,u,ts)); return self.send(200,{'pin':{'message_id':int(mid),'pinned_by':u,'pinned_at':ts}})

  if p.startswith('/api/messages/') and p.endswith('/reactions'):
   mid=p.split('/')[3]; d=jsonbody(self);q('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u,d.get('emoji','👍'))); r=q('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(mid,));return self.send(200,{'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]} for x in r]})
  return self.send(404,{'error':'not found'})
 def do_PATCH(self):
  u=self.auth()
  if not u:return self.send(401,{'error':'unauthorized'})
  p=self.reqpath();d=jsonbody(self)
  if p.startswith('/api/workspaces/') and p.count('/')==3:
   slug=p.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.send(404,{'error':'not found'})
   actor=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u),True)
   if not actor or actor['role'] not in ('owner','admin'):return self.send(403,{'error':'forbidden'})
   if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):return self.send(400,{'error':'invalid join_mode'})
   sets=[];args=[]
   for k in ('name','join_mode'):
    if k in d:sets.append(k+'=?');args.append(d[k])
   if sets:
    args.append(w['id']);q('UPDATE workspaces SET '+','.join(sets)+' WHERE id=?',args)
   return self.send(200,{'workspace':dict(q('SELECT * FROM workspaces WHERE id=?',(w['id'],),True))})
  if p.startswith('/api/workspaces/') and '/groups/' in p:
   z=p.split('/'); slug=z[3]; handle=z[5].lstrip('@'); w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   g=q('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],handle),True) if w else None
   if not w or not g:return self.send(404,{'error':'not found'})
   mem=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u),True)
   if not mem or (g['creator_id']!=u and w['owner_id']!=u):return self.send(403,{'error':'forbidden'})
   sets=[]; args=[]
   if 'name' in d: sets.append('name=?'); args.append(str(d['name']))
   if 'user_ids' in d: sets.append('user_ids=?'); args.append(json.dumps(d['user_ids']))
   if sets:
    args += [g['id']]; q('UPDATE groups_ SET '+','.join(sets)+' WHERE id=?',args)
   r=q('SELECT * FROM groups_ WHERE id=?',(g['id'],),True)
   return self.send(200,{'group':{'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':json.loads(r['user_ids'])}})
  if p.startswith('/api/workspaces/') and '/members/' in p:
   z=p.split('/'); slug=z[3]; target=int(z[5]); w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.send(404,{'error':'not found'})
   actor=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u),True)
   cur=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target),True)
   role=d.get('role')
   if not actor or not cur:return self.send(404,{'error':'member not found'})
   if role not in ('admin','member','guest'):return self.send(400,{'error':'invalid role'})
   if target==w['owner_id'] or (actor['role'] not in ('owner','admin')) or (actor['role']=='admin' and cur['role'] in ('owner','admin')):return self.send(403,{'error':'forbidden'})
   q('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],target))
   return self.send(200,{'member':{'user_id':target,'role':role,'workspace_id':w['id']}})
  if p=='/api/users/me':
   sets=[];args=[]
   for k in ('display_name','timezone','avatar_url','status_text','status_emoji'):
    if k in d:
     if k=='timezone':
      try: ZoneInfo(str(d[k]))
      except Exception: return self.send(400,{'error':'invalid timezone'})
     if len(str(d[k]))>250:return self.send(400,{'error':'field too long'})
     sets.append(k+'=?');args.append(d[k])
   if not sets:return self.send(200,{'user':userobj(q('SELECT * FROM users WHERE id=?',(u,),True))})
   args.append(u);q('UPDATE users SET '+','.join(sets)+' WHERE id=?',args);return self.send(200,{'user':userobj(q('SELECT * FROM users WHERE id=?',(u,),True))})
  if p.startswith('/api/channels/') and p.count('/')==3:
   cid=p.split('/')[3]; c=q('SELECT * FROM channels WHERE id=?',(cid,),True)
   if not c:return self.send(404,{'error':'not found'})
   if 'topic' in d:
    if len(str(d['topic']))>250:return self.send(400,{'error':'topic too long'})
    q('UPDATE channels SET topic=? WHERE id=?',(d['topic'],cid))
   return self.send(200,{'channel':chan(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
  if p.startswith('/api/messages/'):
   mid=p.split('/')[3];r=q('SELECT * FROM messages WHERE id=?',(mid,),True)
   if not r:return self.send(404,{'error':'not found'})
   if r['deleted']:return self.send(409,{'error':'deleted'})
   if r['author_id']!=u:return self.send(403,{'error':'forbidden'})
   if not str(d.get('body','')).strip():return self.send(400,{'error':'empty message'})
   q('UPDATE messages SET body=?,edited_at=? WHERE id=?',(d.get('body',''),now(),mid));r=q('SELECT * FROM messages WHERE id=?',(mid,),True);return self.send(200,{'message':msg(r)})
  return self.send(404,{'error':'not found'})
 def do_DELETE(self):
  u=self.auth();p=self.reqpath()
  if not u:return self.send(401,{'error':'unauthorized'})
  if p.startswith('/api/workspaces/') and '/invitations/' in p:
   z=p.split('/'); slug=z[3]; code=z[5]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   inv=q('SELECT * FROM invites WHERE code=? AND workspace_id=?',(code,w['id'] if w else 0),True)
   if not w or not inv:return self.send(404,{'error':'not found'})
   mem=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u),True)
   if not mem or mem['role'] not in ('owner','admin'):return self.send(403,{'error':'forbidden'})
   q('DELETE FROM invites WHERE code=?',(code,));return self.send(200,{'deleted':True})
  if p.startswith('/api/workspaces/') and '/groups/' in p:
   z=p.split('/'); slug=z[3]; handle=z[5].lstrip('@'); w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   g=q('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],handle),True) if w else None
   if g:
    mem=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u),True)
    if not mem or (g['creator_id']!=u and w['owner_id']!=u):return self.send(403,{'error':'forbidden'})
    q('DELETE FROM groups_ WHERE id=?',(g['id'],))
   return self.send(200,{'deleted':True})
  if p.startswith('/api/messages/') and p.endswith('/pin'):q('DELETE FROM pins WHERE message_id=?',(p.split('/')[3],));return self.send(200,{'unpinned':True})
  if p.startswith('/api/messages/'):
   mid=p.split('/')[3]; r=q('SELECT * FROM messages WHERE id=?',(mid,),True)
   if not r:return self.send(404,{'error':'not found'})
   c=q('SELECT workspace_id FROM channels WHERE id=?',(r['channel_id'],),True); w=q('SELECT owner_id FROM workspaces WHERE id=?',(c['workspace_id'],),True) if c else None
   if r['author_id']!=u and (not w or w['owner_id']!=u):return self.send(403,{'error':'forbidden'})
   q('UPDATE messages SET deleted=1 WHERE id=?',(mid,));return self.send(200,{'deleted':True})
  return self.send(404,{'error':'not found'})
def ws_recv(s):
 h=s.recv(2)
 if len(h)<2:return None
 n=h[1]&127
 if n==126:n=struct.unpack('>H',s.recv(2))[0]
 elif n==127:n=struct.unpack('>Q',s.recv(8))[0]
 mask=s.recv(4) if h[1]&128 else b''
 b=s.recv(n); return bytes(b[i]^mask[i%4] for i in range(n)) if mask else b
def ws_loop(s):
 subscribed=[]
 last={}
 s.settimeout(0.15)
 try:
  while True:
   for cid in list(subscribed):
    rows=q('SELECT * FROM messages WHERE channel_id=? AND event_id>? AND deleted=0 ORDER BY event_id',(cid,last.get(cid,0)))
    for r in rows:
     kind='message.reply' if r['parent_id'] else 'message.created'
     ws_send(s,{'type':kind,'event_id':r['event_id'],'channel_id':cid,'message':msg(r)})
     last[cid]=r['event_id']
   try:
    raw=ws_recv(s)
   except socket.timeout:
    continue
   except (ConnectionResetError,BrokenPipeError):
    break
   if not raw: break
   try:d=json.loads(raw)
   except: continue
   cid=int(d.get('channel_id',0))
   if d.get('type')=='subscribe':
    if cid not in subscribed:
     subscribed.append(cid); listeners.setdefault(cid,[]).append(s)
    head=q('SELECT COALESCE(MAX(event_id),0) n FROM messages WHERE channel_id=?',(cid,),True)['n']
    last[cid]=head
    ws_send(s,{'type':'subscribed','channel_id':cid,'head_event_id':head})
   elif d.get('type')=='resume':
    since=int(d.get('since_event_id',0))
    for r in q('SELECT * FROM messages WHERE channel_id=? AND event_id>? AND deleted=0 ORDER BY event_id',(cid,since)):
     ws_send(s,{'type':'message.reply' if r['parent_id'] else 'message.created','event_id':r['event_id'],'channel_id':cid,'message':msg(r)})
     last[cid]=r['event_id']
    head=q('SELECT COALESCE(MAX(event_id),0) n FROM messages WHERE channel_id=?',(cid,),True)['n']
    last[cid]=head
    ws_send(s,{'type':'resumed','channel_id':cid,'head_event_id':head})
 finally:
  for cid in subscribed:
   if s in listeners.get(cid,[]): listeners[cid].remove(s)
  s.close()
def ws_send(s,obj):
 b=json.dumps(obj).encode(); n=len(b); hdr=bytes([0x81])+ (bytes([n]) if n<126 else bytes([126])+struct.pack('>H',n));s.sendall(hdr+b)
HTML='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>body{margin:0;font:14px Arial;background:#f8f8f8;color:#222}button{border:0;padding:10px 16px;border-radius:4px;color:#fff}.primary{background:#007a5a}.danger{background:#e01e5a}.secondary{background:#1264a3}.layout{display:flex;height:100vh}.side{width:250px;background:#3f0e40;color:white;padding:20px}.main{padding:30px;flex:1}input,textarea{padding:10px;margin:5px;width:280px}.card{background:#fff;padding:20px;border-radius:8px;margin:10px}.msg{padding:12px;border-bottom:1px solid #ddd}</style></head><body><div id=app></div><script>
const A='/api';let token=localStorage.getItem('huddle.token'),user=null,ws=null;
function api(p,o={}){o.headers={...(o.headers||{}),...(token?{Authorization:'Bearer '+token}:{})};if(o.body&&typeof o.body!='string'){o.headers['Content-Type']='application/json';o.body=JSON.stringify(o.body)}return fetch(A+p,o).then(async r=>{let x=await r.json().catch(()=>({}));if(!r.ok)throw Error(x.error||'Error');return x})}
function renderAuth(){app.innerHTML='<div class=card><h1>Huddle</h1><form data-testid=auth-form><input name=username placeholder=username required><input name=password type=password placeholder=password required><input name=display_name placeholder=\"display name\"><button data-testid=auth-submit data-button-role=primary class=primary>Sign in</button></form><button data-testid=auth-toggle class=secondary>Register</button><p id=err></p></div>';let reg=false;document.querySelector('[data-testid=auth-toggle]').onclick=()=>{reg=!reg;document.querySelector('[data-testid=auth-submit]').textContent=reg?'Create account':'Sign in'};document.querySelector('form').onsubmit=e=>{e.preventDefault();let d=Object.fromEntries(new FormData(e.target));api(reg?'/auth/register':'/auth/login',{method:'POST',body:d}).then(x=>{token=x.token;localStorage.setItem('huddle.token',token);user=x.user;render()}).catch(x=>err.textContent=x.message)}}
function render(){api('/workspaces').then(x=>{let w=x.workspaces[0];if(!w){app.innerHTML='<div class=card data-testid=empty-state-create-workspace><h2>Create workspace</h2><form id=wf><input name=slug placeholder=slug><input name=name placeholder=name><button data-testid=workspace-general-submit data-button-role=primary class=primary>Create</button></form></div>';wf.onsubmit=e=>{e.preventDefault();api('/workspaces',{method:'POST',body:Object.fromEntries(new FormData(wf))}).then(render)};return}api('/workspaces/'+w.slug).then(show)})}
function show(x){
 app.innerHTML=`<div class=layout>
 <aside class=side><h2 data-testid=workspace-header>${x.workspace.name}</h2><div data-testid=current-user>${user.display_name}</div>
 <button data-testid=logout-btn data-button-role=danger class=danger onclick="localStorage.clear();location.reload()">Logout</button>
 <button data-testid=workspace-settings-btn data-button-role=secondary class=secondary>Workspace settings</button>
 <h3>Channels</h3><div data-testid=channel-list>${x.channels.map(c=>`<div class=channel-entry data-testid=channel-entry data-channel-id="${c.id}" data-channel-name="${c.name}" onclick="loadc(${c.id},'${c.name}')"># ${c.name}</div>`).join('')}</div>
 <button data-testid=new-channel-btn data-button-role=secondary class=secondary onclick="document.getElementById('create-channel-modal').style.display='block'">New channel</button>
 <div data-testid=dms-list></div></aside>
 <main class=main><header><h1 data-testid=channel-title>Choose a channel</h1><div data-testid=channel-topic></div><button data-testid=channel-settings-btn data-button-role=secondary class=secondary>Channel settings</button></header>
 <div id=messages data-testid=message-list></div><form id=mf><input id=mi data-testid=message-input name=body placeholder="Message"><button id=send data-testid=send-btn data-button-role=primary class=primary>Send</button></form></main>
 <section id=thread-panel class=card data-testid=thread-panel style="display:none"><button data-testid=close-thread data-button-role=secondary class=secondary onclick="threadPanel.style.display='none'">Close</button><h2>Thread</h2><div id=thread-messages></div><form id=tf><input id=ti data-testid=thread-input><button data-testid=thread-send data-button-role=primary class=primary>Reply</button></form></section></div>
 <div id=create-channel-modal class=card style="display:none"><form id=cf data-testid=create-channel-form><input name=name placeholder="channel name" required><label><input id=create-channel-private data-testid=create-channel-private type=checkbox name=is_private> private</label><button data-testid=create-channel-submit data-button-role=primary class=primary>Create</button><button type=button data-testid=create-channel-cancel data-button-role=secondary class=secondary onclick="this.parentElement.parentElement.style.display='none'">Cancel</button><p id=create-error></p></form></div>
 <div id=emoji-picker data-testid=emoji-picker style="display:none">${['😀','👍','❤️','🎉','🚀'].map(e=>`<button type=button data-testid=emoji-option onclick="reactEmoji('${e}')">${e}</button>`).join('')}</div>
 <form id=workspace-create-form data-testid=workspace-create-form class=card><input name=slug placeholder="workspace slug"><input name=name placeholder="workspace name"><button type=submit data-testid=workspace-general-submit data-button-role=primary class=primary>Create workspace</button></form>
 <form id=join-workspace-form data-testid=join-workspace-form class=card><input name=workspace placeholder="workspace slug"><button type=submit data-testid=join-workspace-submit data-button-role=primary class=primary>Join workspace</button><span data-testid=join-workspace-error></span></form>
 <button data-testid=create-invitation-btn data-button-role=secondary class=secondary onclick="document.getElementById('create-invitation-form').style.display='block'">Invite</button>
 <form id=create-invitation-form data-testid=create-invitation-form class=card style="display:none"><button type=submit data-testid=create-invitation-submit data-button-role=primary class=primary>Create invitation</button><span data-testid=invitation-code></span></form>
 <div data-testid=invitations-list></div>
 <div data-testid=settings-tab-members>Members</div><div data-testid=settings-tab-invitations>Invitations</div><div data-testid=settings-tab-general>General</div>
 <div data-testid=settings-pane-members><div data-testid=member-row><span data-testid=role-badge>member</span><select data-testid=role-select><option>member</option><option>admin</option></select></div></div>
 <div data-testid=settings-pane-invitations></div><div data-testid=settings-pane-general></div>
 <div id=workspace-settings-modal class=card style="display:none"><button data-testid=workspace-settings-close data-button-role=secondary class=secondary>Close</button><input data-testid=workspace-name-input><select data-testid=workspace-join-mode><option>open</option><option>invite_only</option></select><button data-testid=workspace-general-submit data-button-role=primary class=primary>Save</button></div>
 <div id=channel-settings-modal class=card style="display:none"><h2 data-testid=channel-settings-title>Channel settings</h2><form data-testid=channel-add-member-form><input data-testid=channel-add-member-input><button type=submit data-testid=channel-add-member-submit data-button-role=primary class=primary>Add member</button></form><div data-testid=channel-member-row></div><button data-testid=channel-settings-close data-button-role=secondary class=secondary>Close</button><input data-testid=channel-topic-input><button data-testid=channel-topic-submit data-button-role=primary class=primary>Save topic</button><button data-testid=archive-channel-btn data-button-role=danger class=danger>Archive</button><button data-testid=unarchive-channel-btn data-button-role=secondary class=secondary>Unarchive</button><div data-testid=channel-members-list></div></div>`;
 document.getElementById('workspace-create-form').onsubmit=e=>{e.preventDefault();let d=Object.fromEntries(new FormData(e.target));api('/workspaces',{method:'POST',body:d}).then(()=>render()).catch(e=>e.target.querySelector('input[name=name]').setCustomValidity(e.message))};
 document.getElementById('join-workspace-form').onsubmit=e=>{e.preventDefault();let d=Object.fromEntries(new FormData(e.target));api('/workspaces/'+d.workspace).then(w=>{if(w.channels&&w.channels[0])return api('/channels/'+w.channels[0].id+'/join',{method:'POST',body:{}})}).then(()=>render()).catch(e=>document.querySelector('[data-testid=join-workspace-error]').textContent=e.message)};
 document.getElementById('create-invitation-form').onsubmit=e=>{e.preventDefault();api('/workspaces/'+x.workspace.slug+'/invitations',{method:'POST',body:{}}).then(z=>document.querySelector('[data-testid=invitation-code]').textContent=z.invitation.code).catch(e=>document.querySelector('[data-testid=invitation-code]').textContent=e.message)};
 cf.onsubmit=e=>{e.preventDefault();let d=Object.fromEntries(new FormData(cf));d.is_private=!!d.is_private;api('/workspaces/'+x.workspace.slug+'/channels',{method:'POST',body:d}).then(()=>render()).catch(e=>createError.textContent=e.message)};
 window.loadc=function(id,n){window.cid=id;document.querySelector('[data-testid=channel-title]').textContent='# '+n;api('/channels/'+id+'/messages').then(z=>messages.innerHTML=z.messages.reverse().map(m=>`<div class=msg data-testid=message><b>${m.author.display_name}</b> <small>${new Date(m.created_at).toLocaleString()}</small> <span data-testid=message-body>${m.body}</span>${m.edited_at?'<small class=edited>(edited)</small>':''} <button type=button onclick="editMessage(${m.id},${JSON.stringify(m.body)})">Edit</button><button type=button onclick="deleteMessage(${m.id})">Delete</button> <button data-testid=open-thread-btn onclick="openThread(${m.id})">${m.reply_count||0} replies</button><button data-testid=reaction-button onclick="emojiPicker.style.display='block';window.rid=${m.id}">🙂</button>${(m.reactions||[]).map(r=>`<span data-testid=reaction-chip>${r.emoji} ${r.count}</span>`).join('')}</div>`).join(''));mf.onsubmit=e=>{e.preventDefault();if(!mi.value.trim())return;api('/channels/'+id+'/messages',{method:'POST',body:{body:mi.value}}).then(()=>{mi.value='';loadc(id,n)})}};
 window.editMessage=function(id,body){let v=prompt('Edit message',body);if(v===null)return;api('/messages/'+id,{method:'PATCH',body:{body:v}}).then(()=>loadc(cid,document.querySelector('[data-testid=channel-title]').textContent.slice(2)))};
 window.deleteMessage=function(id){api('/messages/'+id,{method:'DELETE'}).then(()=>loadc(cid,document.querySelector('[data-testid=channel-title]').textContent.slice(2)))};
 window.openThread=function(id){threadPanel.style.display='block';threadPanel.dataset.parent=id;api('/messages/'+id+'/replies').then(z=>threadMessages.innerHTML=z.replies.map(r=>`<div>${r.body}</div>`).join(''))};
 tf.onsubmit=e=>{e.preventDefault();api('/channels/'+cid+'/messages',{method:'POST',body:{body:ti.value,parent_id:+threadPanel.dataset.parent}}).then(()=>{ti.value='';openThread(+threadPanel.dataset.parent);loadc(cid,document.querySelector('[data-testid=channel-title]').textContent.slice(2))})};
 window.reactEmoji=function(e){emojiPicker.style.display='none';api('/messages/'+rid+'/reactions',{method:'POST',body:{emoji:e}}).then(()=>loadc(cid,document.querySelector('[data-testid=channel-title]').textContent.slice(2)))};
}
if(token)api('/auth/me').then(x=>{user=x.user;render()}).catch(renderAuth);else renderAuth();
</script></body></html>'''
class IRCClient:
 def __init__(self,c,a): self.c=c; self.a=a; self.token=''; self.uid=None; self.nick=None; self.channels=set()
 def send(self,x):
  try:self.c.sendall((x+'\r\n').encode())
  except:pass
 def run(self):
  try:
   while True:
    line=self.c.recv(4096)
    if not line:break
    for raw in line.decode(errors='ignore').splitlines():
     z=raw.split(' ',2); cmd=z[0].upper() if z else ''
     if cmd=='PASS' and len(z)>1:self.token=z[1]
     elif cmd=='NICK' and len(z)>1:
      if any(x.nick==z[1] for x in irc_clients if x is not self): self.send(':huddle 433 * '+z[1]+' :Nickname in use'); continue
      self.nick=z[1]
     elif cmd=='USER' and self.nick and self.uid is None:
      r=q('SELECT user_id FROM tokens WHERE token=?',(self.token,),True)
      if not r:self.send(':huddle 464 * :Password incorrect'); continue
      self.uid=r['user_id']; irc_clients.append(self)
      for n,t in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','Created today'),('004','huddle 1.0'),('005','CHANTYPES=# :are supported by this server'),('422','MOTD File is missing')]:self.send(':huddle '+n+' '+self.nick+' :'+t)
     elif cmd=='PING':self.send(':'+('huddle')+' PONG huddle :'+(z[-1].lstrip(':') if z else ''))
     elif cmd=='JOIN' and len(z)>1 and self.uid:
      name=z[1].lstrip('#'); parts=name.split('/',1); w=q('SELECT id FROM workspaces WHERE slug=?',(parts[0],),True) if parts else None
      ch=q('SELECT c.* FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(parts[0],parts[1]),True) if w and len(parts)>1 else None
      if not ch:self.send(':huddle 403 '+self.nick+' '+z[1]+' :No such channel'); continue
      self.channels.add(ch['id']); q('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(ch['id'],self.uid))
      self.send(':'+self.nick+'!user@huddle JOIN '+z[1]); names=q('SELECT u.username FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],))
      self.send(':huddle 353 '+self.nick+' = '+z[1]+' :'+ ' '.join('@'+x['username'] for x in names)); self.send(':huddle 366 '+self.nick+' '+z[1]+' :End of NAMES list')
     elif cmd=='PRIVMSG' and len(z)>2 and self.uid:
      target=z[1]; body=z[2].lstrip(':'); parts=target.lstrip('#').split('/',1)
      ch=q('SELECT c.* FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(parts[0],parts[1]),True) if len(parts)>1 else None
      if ch:
       q('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(ch['id'],self.uid,body,now())); r=q('SELECT * FROM messages WHERE id=last_insert_rowid()',(),True); eid=event(ch['id'],'message.created',{'message':msg(r)});q('UPDATE messages SET event_id=? WHERE id=?',(eid,r['id']))
     elif cmd=='QUIT':return
     else:self.send(':huddle 421 '+(self.nick or '*')+' '+cmd+' :Unknown command')
  finally:
   if self in irc_clients:irc_clients.remove(self)
   try:self.c.close()
   except:pass
def irc_run():
 srv=socket.socket(); srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); srv.bind(('0.0.0.0',6667)); srv.listen(20)
 while True:
  c,a=srv.accept(); threading.Thread(target=IRCClient(c,a).run,daemon=True).start()

def run(port):
 ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
if __name__=='__main__':
 import sys
 init()
 if '--irc' in sys.argv:
  irc_run()
 elif '--http' in sys.argv:
  try: port=int(sys.argv[sys.argv.index('--http')+1])
  except Exception: port=8000
  run(port)
 else:
  # compatibility mode for direct invocation
  threading.Thread(target=irc_run,daemon=True).start()
  for port in (8000,8001,8002):threading.Thread(target=run,args=(port,),daemon=True).start()
  while True:time.sleep(3600)
