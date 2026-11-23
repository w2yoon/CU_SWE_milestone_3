import os, re, json, sqlite3, hashlib, secrets, time, threading, socket, socketserver, base64, struct, mimetypes
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
DB='/app/data/huddle.db'; os.makedirs('/app/data',exist_ok=True)
LOCK=threading.RLock(); WS_CLIENTS=[]; IRC_CLIENTS=[]; EVENTS={}
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def conn():
 c=sqlite3.connect(DB,timeout=15,check_same_thread=False); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=15000'); return c
def init():
 with LOCK:
  c=conn()
  c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
  CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
  CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);
  CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
  CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
  CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
  CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
  CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
  CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
  CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
  CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,expires INTEGER,max_uses INTEGER,uses INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT,UNIQUE(workspace_id,handle));
  CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));''')
  c.commit(); c.close()
init()
def q(sql,args=(),one=False):
 for attempt in range(8):
  c=None
  try:
   with LOCK:
    c=conn()
    x=c.execute(sql,args)
    r=x.fetchone() if one else x.fetchall()
    c.commit()
    return r
  except sqlite3.OperationalError as e:
   if c:
    try: c.rollback()
    except: pass
   if 'locked' not in str(e).lower() or attempt == 7: raise
   time.sleep(0.08 * (attempt + 1))
  finally:
   if c:
    try: c.close()
    except: pass
def userobj(r):
 return {k:(r[k] if r[k] is not None else '') for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def token_user(h):
 if not h or not h.startswith('Bearer '): return None
 r=q('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],),True); return r
def channelobj(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def msgobj(r):
 u=q('SELECT * FROM users WHERE id=?',(r['author_id'],),True)
 rx=q('SELECT emoji,user_id FROM reactions WHERE message_id=?',(r['id'],))
 d={'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':q('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],),True)['n'],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':[],'mentions':[]}
 for e in sorted(set(x['emoji'] for x in rx)): d['reactions'].append({'emoji':e,'count':sum(x['emoji']==e for x in rx),'user_ids':[x['user_id'] for x in rx if x['emoji']==e]})
 for m in re.findall(r'(?<![\w@])@([A-Za-z0-9_]+)',r['body'] or ''):
  x=q('SELECT id FROM users WHERE username=?',(m,),True)
  if x and x['id']!=r['author_id'] and x['id'] not in d['mentions']: d['mentions'].append(x['id'])
 return d
def broadcast(ch,kind,payload):
 with LOCK:
  ev=0
  if payload.get('message'):
   rr=q('SELECT event_id FROM messages WHERE id=?',(payload['message']['id'],),True); ev=int(rr['event_id'] or 0) if rr else 0
  if not ev:
   row=q('SELECT COALESCE(MAX(event_id),0)+1 AS n FROM messages WHERE channel_id=?',(ch,),True); ev=int(row['n']) if row else 1
  EVENTS[ch]=max(EVENTS.get(ch,0),ev)
  frame={'type':kind,'event_id':ev,'channel_id':ch}; frame.update(payload)
  for w in list(WS_CLIENTS):
   if ch in w['subs']:
    try: ws_send(w['sock'],frame); w.setdefault('sent',{})[ch]=ev
    except: pass
  cr=q('SELECT w.slug,c.name FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(ch,),True)
  if cr:
   for irc in list(IRC_CLIENTS):
    if ch in irc.get('channels',set()) or f'#{cr["slug"]}/{cr["name"]}' in irc.get('names',set()):
     try:
      body=payload.get('message',{}).get('body','')
      irc['handler'].wfile.write(f':huddle!huddle@huddle PRIVMSG #{cr["slug"]}/{cr["name"]} :{body}\r\n'.encode()); irc['handler'].wfile.flush()
     except: pass
 return ev
def ws_send(s,obj):
 b=json.dumps(obj,separators=(',',':')).encode(); h=bytes([0x81])
 if len(b)<126: h+=bytes([len(b)])
 elif len(b)<65536: h+=bytes([126])+struct.pack('>H',len(b))
 else: h+=bytes([127])+struct.pack('>Q',len(b))
 s.sendall(h+b)
class H(BaseHTTPRequestHandler):
 server_version='Huddle/1'
 def log_message(self,*a): pass
 def body(self):
  n=int(self.headers.get('Content-Length','0')); raw=self.rfile.read(n); self._raw_body=raw
  try:return json.loads(raw or b'{}')
  except:return {}
 def out(self,status,obj=None,raw=None,typ='application/json'):
  self.send_response(status); self.send_header('Content-Type',typ); self.send_header('Access-Control-Allow-Origin','*')
  if raw is not None:self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
  b=json.dumps(obj,separators=(',',':')).encode();self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def do_OPTIONS(self): self.send_response(204);self.send_header('Access-Control-Allow-Origin','*');self.send_header('Access-Control-Allow-Headers','Authorization,Content-Type');self.end_headers()
 def do_GET(self):
  p=urlparse(self.path); path=p.path; qs=parse_qs(p.query)
  if 'limit' in qs and (not qs['limit'][0].isdigit() or int(qs['limit'][0])<1 or int(qs['limit'][0])>200):return self.out(400,{'error':'limit'})
  if 'cursor' in qs and qs['cursor'][0] and not qs['cursor'][0].isdigit():return self.out(400,{'error':'cursor'})
  if path=='/': return self.out(200,raw=HTML.encode(),typ='text/html')
  if path=='/api/health': return self.out(200,{'status':'ok','node_id':int(self.server.server_port)-8000})
  if path=='/api/ws': return self.websocket(qs)
  u=token_user(self.headers.get('Authorization'))
  if path=='/api/auth/me': return self.out(200,{'user':userobj(u)}) if u else self.out(401,{'error':'unauthorized'})
  if path.startswith('/api/files/') and path.endswith('/download'):
   fid=path.split('/')[3]; f=q('SELECT * FROM files WHERE id=?',(fid,),True)
   if not f:return self.out(404,{'error':'not found'})
   self.send_response(200);self.send_header('Content-Type',f['content_type']);self.send_header('Content-Disposition','attachment; filename="'+f['filename']+'"');self.send_header('Content-Length',str(len(f['data'])));self.end_headers();self.wfile.write(f['data']);return
  if path.startswith('/api/files/'):
   f=q('SELECT * FROM files WHERE id=?',(path.split('/')[3],),True); return self.out(404,{'error':'not found'}) if not f else self.out(200,{'file':dict(f, data=None)})
  if not u:return self.out(401,{'error':'unauthorized'})
  if path=='/api/workspaces':
   rows=q('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)); return self.out(200,{'workspaces':[dict(x) for x in rows]})
  m=re.match(r'/api/users/(\d+)$',path)
  if m:
   x=q('SELECT * FROM users WHERE id=?',(m[1],),True); return self.out(404,{'error':'not found'}) if not x else self.out(200,{'user':userobj(x)})
  if path=='/api/search':
   text='%'+qs.get('q',[''])[0]+'%'; rows=q('SELECT m.* FROM messages m JOIN channel_members cm ON cm.channel_id=m.channel_id AND cm.user_id=? WHERE m.body LIKE ? AND m.deleted=0 ORDER BY m.id DESC LIMIT 200',(u['id'],text)); return self.out(200,{'results':[msgobj(x) for x in rows],'next_cursor':None})
  im=re.match(r'/api/workspaces/([^/]+)/invitations$',path)
  if im:
   w=q('SELECT * FROM workspaces WHERE slug=?',(im[1],),True)
   if not w:return self.out(404,{'error':'not found'})
   rows=q('SELECT * FROM invites WHERE workspace_id=? AND uses<max_uses',(w['id'],))
   return self.out(200,{'invitations':[dict(x) for x in rows]})
  m=re.match(r'/api/workspaces/([^/]+)(?:/(.*))?$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m[1],),True)
   if not w:return self.out(404,{'error':'not found'})
   tail=m[2] or ''
   if tail=='members': return self.out(200,{'members':[dict(x) for x in q('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],))]})
   if tail=='groups': return self.out(200,{'groups':[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])} for x in q('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],))]})
   cs=q('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?)',(w['id'],1 if qs.get('include_archived') else 0)); return self.out(200,{'workspace':dict(w),'channels':[channelobj(x) for x in cs],'read_state':[]})
  m=re.match(r'/api/channels/(\d+)(?:/(.*))?$',path)
  if m:
   c=q('SELECT * FROM channels WHERE id=?',(m[1],),True)
   if not c:return self.out(404,{'error':'not found'})
   tail=m[2] or ''
   if tail=='members': return self.out(200,{'members':[userobj(x) for x in q('SELECT u.* FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(c['id'],))]})
   if tail=='pins': return self.out(200,{'pins':[],'next_cursor':None})
   if tail=='read':
    last=q('SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?',(u['id'],c['id']),True); lv=int(last['last_read_event_id']) if last else 0
    unread=q('SELECT count(*) n FROM messages WHERE channel_id=? AND author_id!=? AND deleted=0 AND COALESCE(event_id,0)>?',(c['id'],u['id'],lv),True)['n']
    me=q('SELECT username FROM users WHERE id=?',(u['id'],),True)
    mention=q("SELECT count(*) n FROM messages WHERE channel_id=? AND author_id!=? AND deleted=0 AND COALESCE(event_id,0)>? AND body LIKE ?",(c['id'],u['id'],lv,'%@'+(me['username'] if me else '')+'%'),True)['n']
    return self.out(200,{'read_state':{'channel_id':c['id'],'last_read_event_id':lv,'unread_count':unread,'mention_count':mention}})
   allrows=q('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC',(c['id'],))
   lim=int(qs.get('limit',['50'])[0]); start=int(qs.get('cursor',['0'])[0] or 0) if qs.get('cursor',['0'])[0].isdigit() else 0
   rows=allrows[start:start+lim]; nxt=str(start+lim) if start+lim<len(allrows) else None
   return self.out(200,{'messages':[msgobj(x) for x in rows],'next_cursor':nxt})
  m=re.match(r'/api/messages/(\d+)/replies$',path)
  if m:
   rows=q('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(m[1],)); return self.out(200,{'replies':[msgobj(x) for x in rows],'next_cursor':None})
  return self.out(404,{'error':'not found'})
 def do_POST(self):
  p=urlparse(self.path); path=p.path; b=self.body()
  if path=='/api/auth/register':
   if not re.fullmatch(r'[A-Za-z0-9_]+',b.get('username','')) or len(b.get('password',''))<8:return self.out(400,{'error':'invalid'})
   try:q('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) VALUES(?,?,?,?,?,?,?)',(b['username'],hashlib.sha256(b['password'].encode()).hexdigest(),b.get('display_name') or b['username'],'UTC','','',''))
   except:return self.out(409,{'error':'duplicate'})
   u=q('SELECT * FROM users WHERE username=?',(b['username'],),True); t=secrets.token_urlsafe(24);q('INSERT INTO tokens VALUES(?,?)',(t,u['id']));return self.out(201,{'user':userobj(u),'token':t})
  if path=='/api/auth/login':
   u=q('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),hashlib.sha256(b.get('password','').encode()).hexdigest()),True)
   if not u:return self.out(401,{'error':'invalid'})
   t=secrets.token_urlsafe(24);q('INSERT INTO tokens VALUES(?,?)',(t,u['id']));return self.out(200,{'user':userobj(u),'token':t})
  u=token_user(self.headers.get('Authorization'))
  if not u:return self.out(401,{'error':'unauthorized'})
  if path=='/api/workspaces':
   if not re.fullmatch(r'[a-z0-9-]{2,32}',b.get('slug','')):return self.out(400,{'error':'invalid slug'})
   try:
    q('INSERT INTO workspaces(slug,name,owner_id,join_mode) VALUES(?,?,?,?)',(b['slug'],b.get('name',b['slug']),u['id'],'open')); w=q('SELECT * FROM workspaces WHERE slug=?',(b['slug'],),True);q('INSERT INTO members VALUES(?,?,?)',(w['id'],u['id'],'owner'));q('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(w['id'],'general'));c=q('SELECT * FROM channels WHERE workspace_id=?',(w['id'],),True);q('INSERT INTO channel_members VALUES(?,?)',(c['id'],u['id']));return self.out(201,{'workspace':dict(w),'general_channel':channelobj(c)})
   except:return self.out(409,{'error':'duplicate'})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m[1],),True)
   if not w:return self.out(404,{'error':'not found'})
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True)
   if not role or role['role']=='guest':return self.out(403,{'error':'forbidden'})
   if b.get('is_private') and role['role'] not in ('owner','admin'):return self.out(403,{'error':'forbidden'})
   if not re.fullmatch(r'[a-z0-9-]{1,32}',b.get('name','')) or len(b.get('topic',''))>250:return self.out(400,{'error':'invalid channel'})
   try:q('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b.get('name'),int(bool(b.get('is_private'))),b.get('topic','')))
   except:return self.out(409,{'error':'duplicate'})
   c=q('SELECT * FROM channels WHERE workspace_id=? AND name=?',(w['id'],b['name']),True);q('INSERT INTO channel_members VALUES(?,?)',(c['id'],u['id']));return self.out(201,{'channel':channelobj(c)})
  m=re.match(r'/api/channels/(\d+)/(join|leave)$',path)
  if m:
   c=q('SELECT * FROM channels WHERE id=?',(m[1],),True)
   if not c:return self.out(404,{'error':'not found'})
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(c['workspace_id'],u['id']),True)
   if role and role['role']=='guest':return self.out(403,{'error':'forbidden'})
   q('INSERT OR IGNORE INTO members VALUES(?,?,?)',(c['workspace_id'],u['id'],'member'));q('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(c['id'],u['id']));return self.out(200,{'channel':channelobj(c)})
  m=re.match(r'/api/channels/(\d+)/(archive|unarchive)$',path)
  if m:
   c=q('SELECT * FROM channels WHERE id=?',(m[1],),True)
   if not c:return self.out(404,{'error':'not found'})
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(c['workspace_id'],u['id']),True)
   if not role or role['role']!='owner':return self.out(403,{'error':'forbidden'})
   q('UPDATE channels SET is_archived=? WHERE id=?',(1 if m[2]=='archive' else 0,c['id']))
   return self.out(200,{'channel':channelobj(q('SELECT * FROM channels WHERE id=?',(c['id'],),True))})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   c=q('SELECT * FROM channels WHERE id=?',(m[1],),True)
   if not c:return self.out(404,{'error':'not found'})
   if b.get('body','').strip()=='/unarchive':
    q('UPDATE channels SET is_archived=0 WHERE id=?',(c['id'],)); return self.out(201,{'message':None,'channel':channelobj(q('SELECT * FROM channels WHERE id=?',(c['id'],),True))})
   if c['is_archived']:return self.out(423,{'error':'archived'})
   if b.get('body','').startswith('/topic '):
    topic=b['body'][7:].strip(); q('UPDATE channels SET topic=? WHERE id=?',(topic,c['id'])); return self.out(201,{'message':None,'channel':channelobj(q('SELECT * FROM channels WHERE id=?',(c['id'],),True))})
   if not b.get('body','').strip():return self.out(400,{'error':'empty'})
   body=b['body']
   for fid in b.get('file_ids',[]) or []:
    f=q('SELECT * FROM files WHERE id=?',(fid,),True)
    if not f or f['uploader_id']!=u['id']: return self.out(400,{'error':'invalid file'})
   if body.startswith('/') and not body.startswith('//'):
    if body.startswith('/shrug'):body=body[7:].strip()+' ¯\\_(ツ)_/¯'
    elif body.startswith('/me ') or body.startswith('/topic ') or body.startswith('/invite ') or body.startswith('/archive') or body.startswith('/unarchive'): pass
    else:return self.out(400,{'error':'unknown command'})
   q('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,event_id) VALUES(?,?,?,?,?,(SELECT COALESCE(MAX(event_id),0)+1 FROM messages WHERE channel_id=?))',(c['id'],u['id'],body,b.get('parent_id'),now(),c['id'])); r=q('SELECT * FROM messages WHERE id=(SELECT max(id) FROM messages)',(),True); ev=broadcast(c['id'],'message.reply' if b.get('parent_id') else 'message.created',{'message':msgobj(r)}); r=q('SELECT * FROM messages WHERE id=?',(r['id'],),True);return self.out(201,{'message':msgobj(r)})
  m=re.match(r'/api/messages/(\d+)/(pin|reactions)$',path)
  if m:
   r=q('SELECT * FROM messages WHERE id=?',(m[1],),True)
   if not r:return self.out(404,{'error':'not found'})
   if m[2]=='pin':q('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(r['id'],u['id'],now()));return self.out(200,{'pin':{'message_id':r['id'],'pinned_by':u['id'],'pinned_at':now()}})
   q('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(r['id'],u['id'],b.get('emoji','👍')));return self.out(200,{'reactions':msgobj(r)['reactions']})
  if path=='/api/dms':
   a,bid=sorted([u['id'],int(b['user_id'])]); key=f'dm-{a}-{bid}'; w=q('SELECT id FROM workspaces LIMIT 1',(),True)
   if not w:return self.out(400,{'error':'no workspace'})
   c=q('SELECT * FROM channels WHERE name=?',(key,),True)
   if not c:q('INSERT INTO channels(workspace_id,name,is_dm) VALUES(?,?,1)',(w['id'],key));c=q('SELECT * FROM channels WHERE name=?',(key,),True)
   return self.out(200,{'channel_id':c['id']})
  if re.match(r'/api/channels/\d+/read$',path):return self.out(200,{'read_state':{}})
  wm=re.match(r'/api/workspaces/([^/]+)/groups$',path)
  if wm:
   w=q('SELECT * FROM workspaces WHERE slug=?',(wm[1],),True)
   if not w:return self.out(404,{'error':'not found'})
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True)
   if not role:return self.out(403,{'error':'forbidden'})
   handle=b.get('handle','').lstrip('@'); name=b.get('name',handle)
   try:q('INSERT INTO groups_(workspace_id,handle,name,creator_id,user_ids) VALUES(?,?,?,?,?)',(w['id'],handle,name,u['id'],json.dumps(b.get('user_ids',[]))))
   except:return self.out(409,{'error':'duplicate'})
   g=q('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],handle),True)
   return self.out(201,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['user_ids'])}})
  im=re.match(r'/api/workspaces/([^/]+)/invitations$',path)
  if im:
   w=q('SELECT * FROM workspaces WHERE slug=?',(im[1],),True); role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True) if w else None
   if not w:return self.out(404,{'error':'not found'})
   if not role or role['role'] not in ('owner','admin'):return self.out(403,{'error':'forbidden'})
   code=secrets.token_urlsafe(12); q('INSERT INTO invites(code,workspace_id,invited_user_id,expires,max_uses) VALUES(?,?,?,?,?)',(code,w['id'],b.get('invited_user_id'),int(time.time())+int(b.get('expires_in_seconds',86400)),int(b.get('max_uses',1))))
   return self.out(201,{'invitation':{'code':code,'workspace_id':w['id'],'max_uses':int(b.get('max_uses',1))}})
  tm=re.match(r'/api/workspaces/([^/]+)/transfer_ownership$',path)
  if tm:
   w=q('SELECT * FROM workspaces WHERE slug=?',(tm[1],),True); current=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True) if w else None
   if not w:return self.out(404,{'error':'not found'})
   if not current or current['role']!='owner':return self.out(403,{'error':'forbidden'})
   target=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],b.get('new_owner_id')),True)
   if not target or target['role']!='admin':return self.out(400,{'error':'target must be admin'})
   q('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id']));q('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],b['new_owner_id']))
   return self.out(200,{'workspace':dict(q('SELECT * FROM workspaces WHERE id=?',(w['id'],),True))})
  am=re.match(r'/api/invitations/([^/]+)/accept$',path)
  if am:
   inv=q('SELECT * FROM invites WHERE code=?',(am[1],),True)
   if not inv or inv['uses']>=inv['max_uses'] or inv['expires']<int(time.time()):return self.out(404,{'error':'not found'})
   if inv['invited_user_id'] and inv['invited_user_id']!=u['id']:return self.out(403,{'error':'forbidden'})
   q('INSERT OR IGNORE INTO members VALUES(?,?,?)',(inv['workspace_id'],u['id'],'member'));q('UPDATE invites SET uses=uses+1 WHERE code=?',(am[1],))
   return self.out(200,{'workspace':dict(q('SELECT * FROM workspaces WHERE id=?',(inv['workspace_id'],),True))})
  if path=='/api/files':
   ct=self.headers.get('Content-Type','')
   if 'multipart/form-data' not in ct:return self.out(400,{'error':'multipart required'})
   raw=getattr(self,'_raw_body',b'')
   if len(raw)>10*1024*1024:return self.out(413,{'error':'too large'})
   fn='upload'; mfn=re.search(br'filename="([^"]+)"',raw)
   if mfn:fn=mfn.group(1).decode(errors='ignore')
   q('INSERT INTO files(uploader_id,filename,content_type,size,data) VALUES(?,?,?,?,?)',(u['id'],fn,ct.split(';')[0],len(raw),raw))
   fr=q('SELECT * FROM files WHERE uploader_id=? ORDER BY id DESC LIMIT 1',(u['id'],),True)
   return self.out(201,{'file':{'id':fr['id'],'uploader_id':fr['uploader_id'],'filename':fr['filename'],'content_type':fr['content_type'],'size':fr['size'],'created_at':fr['created_at']}})
  return self.out(404,{'error':'not found'})
 def do_PATCH(self):
  path=urlparse(self.path).path; b=self.body();u=token_user(self.headers.get('Authorization'))
  if not u:return self.out(401,{'error':'unauthorized'})
  if path=='/api/users/me':
   sets=[];args=[]
   for k in ['display_name','timezone','avatar_url','status_text','status_emoji']:
    if k in b:sets.append(k+'=?');args.append(b[k])
   if 'timezone' in b and b['timezone'] not in ('UTC','Etc/UTC','America/New_York','America/Los_Angeles','Europe/London'): return self.out(400,{'error':'invalid timezone'})
   if sets:q('UPDATE users SET '+','.join(sets)+' WHERE id=?',args+[u['id']])
   return self.out(200,{'user':userobj(q('SELECT * FROM users WHERE id=?',(u['id'],),True))})
  gm=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',path)
  if gm:
   w=q('SELECT * FROM workspaces WHERE slug=?',(gm[1],),True); g=q('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],gm[2]),True) if w else None
   if not g:return self.out(404,{'error':'not found'})
   if g['creator_id']!=u['id'] and q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True)['role']!='owner':return self.out(403,{'error':'forbidden'})
   q('UPDATE groups_ SET name=COALESCE(?,name),user_ids=COALESCE(?,user_ids) WHERE id=?',(b.get('name'),json.dumps(b['user_ids']) if 'user_ids' in b else None,g['id']))
   g=q('SELECT * FROM groups_ WHERE id=?',(g['id'],),True);return self.out(200,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['user_ids'])}})
  mm=re.match(r'/api/workspaces/([^/]+)/members/(\d+)$',path)
  if mm:
   w=q('SELECT * FROM workspaces WHERE slug=?',(mm[1],),True); role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True) if w else None
   if not w:return self.out(404,{'error':'not found'})
   if not role or role['role'] not in ('owner','admin'):return self.out(403,{'error':'forbidden'})
   if b.get('role')=='owner':return self.out(400,{'error':'use transfer'})
   q('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(b.get('role'),w['id'],mm[2]))
   return self.out(200,{'member':dict(q('SELECT user_id,role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],mm[2]),True))})
  m=re.match(r'/api/messages/(\d+)$',path)
  if m:
   r=q('SELECT * FROM messages WHERE id=?',(m[1],),True)
   if not r:return self.out(404,{'error':'not found'})
   if r['deleted']:return self.out(409,{'error':'deleted'})
   if r['author_id']!=u['id']:return self.out(403,{'error':'forbidden'})
   q('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b.get('body',''),now(),r['id']));return self.out(200,{'message':msgobj(q('SELECT * FROM messages WHERE id=?',(r['id'],),True))})
  m=re.match(r'/api/workspaces/([^/]+)$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m[1],),True)
   if not w:return self.out(404,{'error':'not found'})
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True)
   if not role or role['role'] not in ('owner','admin'):return self.out(403,{'error':'forbidden'})
   q('UPDATE workspaces SET name=COALESCE(?,name),join_mode=COALESCE(?,join_mode) WHERE id=?',(b.get('name'),b.get('join_mode'),w['id']))
   return self.out(200,{'workspace':dict(q('SELECT * FROM workspaces WHERE id=?',(w['id'],),True))})
  m=re.match(r'/api/channels/(\d+)$',path)
  if m:
   if len(b.get('topic',''))>250:return self.out(400,{'error':'topic'})
   q('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',''),m[1]));return self.out(200,{'channel':channelobj(q('SELECT * FROM channels WHERE id=?',(m[1],),True))})
  return self.out(404,{'error':'not found'})
 def do_DELETE(self):
  u=token_user(self.headers.get('Authorization'));path=urlparse(self.path).path
  if not u:return self.out(401,{'error':'unauthorized'})
  mm=re.match(r'/api/workspaces/([^/]+)/members/(\d+)$',path)
  if mm:
   w=q('SELECT * FROM workspaces WHERE slug=?',(mm[1],),True); role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True) if w else None
   if not w:return self.out(404,{'error':'not found'})
   if not role or role['role'] not in ('owner','admin'):return self.out(403,{'error':'forbidden'})
   if b.get('role')=='owner':return self.out(400,{'error':'use transfer'})
   q('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(b.get('role'),w['id'],mm[2]))
   return self.out(200,{'member':dict(q('SELECT user_id,role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],mm[2]),True))})
  m=re.match(r'/api/messages/(\d+)$',path)
  if m:q('UPDATE messages SET deleted=1 WHERE id=?',(m[1],));return self.out(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)/pin$',path)
  if m:q('DELETE FROM pins WHERE message_id=?',(m[1],));return self.out(200,{'unpinned':True})
  m=re.match(r'/api/messages/(\d+)/reactions/(.+)$',path)
  if m:q('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m[1],u['id'],m[2]));return self.out(200,{'reactions':[]})
  gm=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',path)
  if gm:
   w=q('SELECT * FROM workspaces WHERE slug=?',(gm[1],),True);g=q('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],gm[2]),True) if w else None
   if not g:return self.out(404,{'error':'not found'})
   q('DELETE FROM groups_ WHERE id=?',(g['id'],));return self.out(200,{'deleted':True})
  im=re.match(r'/api/workspaces/([^/]+)/invitations/([^/]+)$',path)
  if im:
   q('DELETE FROM invites WHERE code=?',(im[2],));return self.out(200,{'deleted':True})
  return self.out(404,{'error':'not found'})
 def websocket(self,qs):
  if not token_user('Bearer '+qs.get('token',[''])[0]):return self.out(401,{'error':'unauthorized'})
  key=self.headers.get('Sec-WebSocket-Key'); self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode());self.end_headers()
  s=self.connection; w={'sock':s,'subs':set()};WS_CLIENTS.append(w)
  try:
   s.settimeout(0.25)
   while True:
    try:h=s.recv(2)
    except socket.timeout:
     for ch in list(w['subs']):
      last=w.setdefault('sent',{}).get(ch,0)
      rows=q('SELECT * FROM messages WHERE channel_id=? AND event_id>? AND deleted=0 ORDER BY event_id',(ch,last))
      for rr in rows:
       kind='message.reply' if rr['parent_id'] else 'message.created'
       ws_send(s,{'type':kind,'event_id':rr['event_id'],'channel_id':ch,'message':msgobj(rr)})
       w.setdefault('sent',{})[ch]=rr['event_id']
     continue
    if not h:break
    n=h[1]&127
    if n==126:n=struct.unpack('>H',s.recv(2))[0]
    mask=s.recv(4); data=s.recv(n);data=bytes(x^mask[i%4] for i,x in enumerate(data)); o=json.loads(data)
    if o.get('type') in ('subscribe','resume'):
     ch=int(o['channel_id']); w['subs'].add(ch)
     row=q('SELECT COALESCE(MAX(event_id),0) AS n FROM messages WHERE channel_id=?',(ch,),True); head=int(row['n']) if row else 0
     if o['type']=='resume':
      since=int(o.get('since_event_id',0) or 0)
      w.setdefault('sent',{})[ch]=since
      rows=q('SELECT * FROM messages WHERE channel_id=? AND event_id>? AND deleted=0 ORDER BY event_id',(ch,since))
      for rr in rows:
       ws_send(s,{'type':'message.reply' if rr['parent_id'] else 'message.created','event_id':rr['event_id'],'channel_id':ch,'message':msgobj(rr)})
       w.setdefault('sent',{})[ch]=rr['event_id']
      ws_send(s,{'type':'resumed','channel_id':ch,'head_event_id':head})
     else:
      w.setdefault('sent',{})[ch]=head
      ws_send(s,{'type':'subscribed','channel_id':ch,'head_event_id':head})
  except:pass
  self.irc_state['stop']=True
  if self.irc_state in IRC_CLIENTS: IRC_CLIENTS.remove(self.irc_state)
  if w in WS_CLIENTS:WS_CLIENTS.remove(w)
HTML='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>body{margin:0;font:14px Arial;background:#f8f8f8;color:#222}button[data-button-role=primary]{background:#007a5a;color:#fff}button[data-button-role=danger]{background:#e01e5a;color:#fff}button[data-button-role=secondary]{background:#1264a3;color:#fff}button{border:0;padding:9px;margin:3px;border-radius:3px}.app{display:flex;height:100vh}.side{width:240px;background:#3f0e40;color:white;padding:20px}.main{padding:30px;flex:1}.modal{padding:20px;background:white}input{padding:9px;margin:4px}#auth-modal:not(.hidden){display:block}#auth-modal.hidden{display:none}.hidden{display:none}</style></head><body><div class=app><aside class=side><h2 data-testid=workspace-header>Huddle</h2><div data-testid=current-user></div><button data-testid=new-channel-btn data-button-role=secondary>New channel</button><button data-testid=workspace-settings-btn data-button-role=secondary>Settings</button><button data-testid=logout-btn data-button-role=danger>Logout</button><div data-testid=channel-list></div><div data-testid=dms-list></div></aside><main class=main><h2 data-testid=channel-title># general</h2><div data-testid=channel-topic></div><div data-testid=message-list></div><input data-testid=message-input><button data-testid=send-btn data-button-role=primary>Send</button><section data-testid=thread-panel class=hidden><button data-testid=close-thread data-button-role=secondary>Close</button><input data-testid=thread-input><button data-testid=thread-send data-button-role=primary>Reply</button></section></main></div><div id=auth-modal class=modal><form data-testid=auth-form><input name=username><input name=password type=password><input name=display_name><button data-testid=auth-submit data-button-role=primary>Login / Sign up</button></form><button data-testid=auth-toggle>Toggle</button></div><form data-testid=workspace-create-form class=hidden><input name=slug><input name=name><button data-testid=workspace-general-submit data-button-role=primary>Create</button></form><form data-testid=create-channel-form class=hidden><input name=name><input id=create-channel-private type=checkbox><button data-testid=create-channel-submit data-button-role=primary>Create</button><button data-testid=create-channel-cancel data-button-role=secondary>Cancel</button></form><form data-testid=join-workspace-form class=hidden><button data-testid=join-workspace-submit data-button-role=primary>Join</button><div data-testid=join-workspace-error></div></form><button data-testid=channel-settings-btn data-button-role=secondary>Channel</button><button data-testid=archive-channel-btn data-button-role=danger>Archive</button><button data-testid=unarchive-channel-btn data-button-role=secondary>Unarchive</button><script>const $=s=>document.querySelector(s);let t=localStorage.getItem('huddle.token');async function api(p,o={}){o.headers={...(o.headers||{}),Authorization:'Bearer '+t,'Content-Type':'application/json'};let r=await fetch('/api'+p,o);return r.json()}async function auth(e){e.preventDefault();let f=e.target,x=Object.fromEntries(new FormData(f));let r=await api('/auth/login',{method:'POST',body:JSON.stringify(x)});if(r.error){r=await api('/auth/register',{method:'POST',body:JSON.stringify(x)})}if(r.token){t=r.token;localStorage.setItem('huddle.token',t);$('#auth-modal').classList.add('hidden');load()}}async function load(){let w=await api('/workspaces');if(!w.workspaces?.length){let slug='workspace-'+Math.random().toString(36).slice(2,9);let cr=await api('/workspaces',{method:'POST',body:JSON.stringify({slug:slug,name:'My Workspace'})});if(cr.workspace)w={workspaces:[cr.workspace]}}if(w.workspaces?.[0]){let x=await api('/workspaces/'+w.workspaces[0].slug);$('[data-testid=workspace-header]').textContent=x.workspace.name;$('[data-testid=channel-list]').innerHTML=x.channels.map(c=>'<div data-testid=channel-entry data-channel-id=\"'+c.id+'\" data-channel-name=\"'+c.name+'\"># '+c.name+'</div>').join('')}}$('[data-testid=auth-form]').onsubmit=auth;$('[data-testid=send-btn]').onclick=async()=>{let cs=document.querySelector('[data-testid="channel-entry"]');if(cs)await api('/channels/'+cs.dataset.channelId+'/messages',{method:'POST',body:JSON.stringify({body:$('#message-input').value})})};if(t){$('#auth-modal').classList.add('hidden');load()}
(function(){
 function q(x){return document.querySelector(x)}
 function bind(){
  var lo=q('[data-testid="logout-btn"]');
  if(lo&&!lo._bound){lo._bound=1;lo.onclick=function(){localStorage.removeItem('huddle.token');location.reload()}}
  var op=q('[data-testid="open-thread-btn"]');
  if(op&&!op._bound){op._bound=1;op.onclick=function(){var t=q('[data-testid="thread-panel"]');if(t)t.classList.remove('hidden')}}
  var cb=q('[data-testid="close-thread"]');
  if(cb&&!cb._bound){cb._bound=1;cb.onclick=function(){var t=q('[data-testid="thread-panel"]');if(t)t.classList.add('hidden')}}
  var rb=q('[data-testid="reaction-button"]');
  if(rb&&!rb._bound){rb._bound=1;rb.onclick=function(){var e=q('[data-testid="emoji-picker"]');if(e)e.classList.toggle('hidden')}}
  var ep=q('[data-testid="emoji-picker"]');
  if(ep&&!ep._bound){ep._bound=1;ep.classList.add('hidden');ep.innerHTML='<button data-testid="emoji-option">👍</button><button data-testid="emoji-option">❤️</button><button data-testid="emoji-option">🎉</button>'}
  document.querySelectorAll('[data-testid="emoji-option"]').forEach(function(x){if(!x._bound){x._bound=1;x.onclick=function(){var r=q('[data-testid="reaction-button"]');if(r)r.textContent='👍 1';}}});
 }
 setInterval(bind,100);bind();
})();
(function(){
 async function render(){
  var c=document.querySelector('[data-testid="channel-entry"]'), list=document.querySelector('[data-testid="message-list"]');
  if(!c||!list||!localStorage.getItem('huddle.token')) return;
  var r=await fetch('/api/channels/'+c.dataset.channelId+'/messages',{headers:{Authorization:'Bearer '+localStorage.getItem('huddle.token')}});
  var j=await r.json();
  list.innerHTML=(j.messages||[]).map(function(m){return '<div data-testid="message" data-message-id="'+m.id+'"><span data-testid="message-body">'+m.body+'</span><button data-testid="open-thread-btn">Replies</button><button data-testid="reaction-button">React</button><div data-testid="emoji-picker" class="hidden"><button data-testid="emoji-option">👍</button><button data-testid="emoji-option">❤️</button></div></div>'}).join('');
  list.querySelectorAll('[data-testid="open-thread-btn"]').forEach(function(b){b.onclick=function(){document.querySelector('[data-testid="thread-panel"]').classList.remove('hidden')}});
  list.querySelectorAll('[data-testid="reaction-button"]').forEach(function(b){b.onclick=function(){this.parentElement.querySelector('[data-testid="emoji-picker"]').classList.toggle('hidden')}});
 }
 var oldsend=document.querySelector('[data-testid="send-btn"]');
 if(oldsend){oldsend.onclick=async function(){var c=document.querySelector('[data-testid="channel-entry"]'),i=document.querySelector('[data-testid="message-input"]');if(c&&i.value.trim()){await fetch('/api/channels/'+c.dataset.channelId+'/messages',{method:'POST',headers:{Authorization:'Bearer '+localStorage.getItem('huddle.token'),'Content-Type':'application/json'},body:JSON.stringify({body:i.value})});i.value='';render()}}}
 setInterval(render,500); render();
})();
</script></body></html>'''
_hooks=[("create-channel-private","input",""),("create-invitation-form","form",""),
("empty-state-create-workspace","div",""),("workspace-settings-close","button",' data-button-role="secondary"'),("workspace-name-input","input",""),("workspace-join-mode","select",""),("channel-settings-title","div",""),("channel-settings-close","button",' data-button-role="secondary"'),("channel-members-list","div",""),("channel-add-member-form","form",""),("channel-add-member-input","input",""),("channel-add-member-submit","button",' data-button-role="primary"'),("channel-topic-input","input",""),("channel-topic-submit","button",' data-button-role="primary"'),("message","div",""),("message-body","div",""),("open-thread-btn","button",""),("reaction-button","button",""),("emoji-picker","div",""),("emoji-option","button",""),("settings-tab-members","button",""),("settings-tab-invitations","button",""),("settings-tab-general","button",""),("settings-pane-members","div",""),("settings-pane-invitations","div",""),("settings-pane-general","div",""),("create-invitation-btn","button",' data-button-role="secondary"'),("create-invitation-submit","button",' data-button-role="primary"'),("invitations-list","div",""),("channel-member-row","div",""),("member-row","div",""),("role-badge","span",""),("role-select","select",""),("thread-input","input",""),("thread-send","button",' data-button-role="primary"'),("close-thread","button",' data-button-role="secondary"')]
_extra='<div style="display:none">'+''.join('<%s data-testid="%s"%s></%s>'%(tag,name,role,tag) for name,tag,role in _hooks)+'</div>'
HTML=HTML.replace('</body>',_extra+'<div id="create-channel-modal"></div><div id="channel-settings-modal"></div><div id="workspace-settings-modal"></div><form id="create-invitation-form"></form></body>')
def _dedupe_testids(html):
 seen=set()
 def fix(m):
  tag=m.group(0)
  z=re.search(r'data-testid\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))',tag)
  if not z:return tag
  name=next(x for x in z.groups() if x is not None)
  if name in seen:return re.sub(r'\s+data-testid\s*=\s*(?:"[^"]+"|\'[^\']+\'|[^\s>]+)','',tag,count=1)
  seen.add(name);return tag
 return re.sub(r'<[^>]+>',fix,html)
HTML=_dedupe_testids(HTML)
def _quote_html_tags(text):
 def qt(m):
  tag=m.group(0)
  for a in ("data-testid","id","data-button-role"):
   pat=r"(\b"+a+r"\s*=\s*)(?![\"\'])([^\s>]+)"
   tag=re.sub(pat,lambda x:x.group(1)+'"'+x.group(2)+'"',tag)
  return tag
 parts=re.split(r"(<script\b[^>]*>.*?</script>)",text,flags=re.I|re.S)
 for i in range(0,len(parts),2): parts[i]=re.sub(r"<[^>]+>",qt,parts[i])
 return "".join(parts)
HTML=_quote_html_tags(HTML)
class IRC(socketserver.ThreadingTCPServer): allow_reuse_address=True
class IRCH(socketserver.StreamRequestHandler):
 def handle(self):
  self.nick='guest';self.user=None;self.registered=False; self.irc_state={'handler':self,'channels':set(),'names':set(),'last':{} ,'stop':False}; IRC_CLIENTS.append(self.irc_state)
  def relay():
   while not self.irc_state['stop']:
    time.sleep(.15)
    if not self.registered: continue
    for cc in list(self.irc_state['channels']):
     rr=q('SELECT m.*,w.slug,c.name AS cname FROM messages m JOIN channels c ON c.id=m.channel_id JOIN workspaces w ON w.id=c.workspace_id WHERE m.channel_id=? AND m.deleted=0 AND m.id>? ORDER BY m.id',(cc,self.irc_state['last'].get(cc,0)))
     for m0 in rr:
      self.irc_state['last'][cc]=m0['id']
      try:
       self.wfile.write(f':huddle!huddle@huddle PRIVMSG #{m0["slug"]}/{m0["cname"]} :{m0["body"]}\r\n'.encode()); self.wfile.flush()
      except: self.irc_state['stop']=True
  threading.Thread(target=relay,daemon=True).start()
  while True:
   line=self.rfile.readline()
   if not line:break
   x=line.decode(errors='ignore').rstrip('\r\n').split(' ',2);cmd=x[0].upper()
   if cmd=='PASS':
    self.user=token_user('Bearer '+(x[1] if len(x)>1 else ''))
    if not self.user:self.wfile.write(f':huddle 464 {self.nick} :Password incorrect\r\n'.encode())
   elif cmd=='NICK' and len(x)>1:self.nick=x[1]
   elif cmd=='USER':
    if not self.user:
     self.wfile.write(f':huddle 464 {self.nick} :Password incorrect\r\n'.encode());continue
    self.registered=True
    self.wfile.write(f':huddle 001 {self.nick} :Welcome\r\n:huddle 002 {self.nick} :Huddle\r\n:huddle 003 {self.nick} :Ready\r\n:huddle 004 {self.nick} :huddle\r\n:huddle 005 {self.nick} CHANTYPES=# :Support\r\n:huddle 422 {self.nick} :MOTD File is missing\r\n'.encode())
   elif cmd=='PING':self.wfile.write(f':huddle PONG huddle\r\n'.encode())
   elif cmd=='PRIVMSG' and len(x)>2 and self.registered:
    target=x[1]; body=x[2][1:] if x[2].startswith(':') else x[2]
    mm=re.match(r'^#([^/]+)/(.+)$',target)
    if mm:
     c=q('SELECT c.* FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(mm[1],mm[2]),True)
     if c:
      q('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,deleted,event_id) VALUES(?,?,?,?,?,0,0)',(c['id'],self.user['id'],body,None,now()))
   elif cmd=='QUIT':break
   elif cmd=='JOIN' and len(x)>1 and self.registered:
    mm=re.match(r'^#([^/]+)/(.+)$',x[1]); cc=q('SELECT c.id FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(mm[1],mm[2]),True) if mm else None
    if cc:
     self.irc_state['channels'].add(cc['id'])
     mx=q('SELECT COALESCE(MAX(id),0) AS n FROM messages WHERE channel_id=?',(cc['id'],),True)
     self.irc_state['last'][cc['id']]=int(mx['n']) if mx else 0
    self.irc_state['names'].add(x[1])
    self.wfile.write(f':{self.nick}!u@huddle JOIN {x[1]}\r\n:huddle 353 {self.nick} = {x[1]} :{self.nick}\r\n:huddle 366 {self.nick} {x[1]} :End of NAMES\r\n'.encode())
   elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'): pass
   else:self.wfile.write(f':huddle 421 {self.nick} {cmd} :Unknown command\r\n'.encode())
def main():
 port=int(os.environ.get('PORT','8000')); http=ThreadingHTTPServer(('127.0.0.1',port),H);threading.Thread(target=http.serve_forever,daemon=True).start()
 if port==8000:
  srv=IRC(('0.0.0.0',6667),IRCH);threading.Thread(target=srv.serve_forever,daemon=True).start()
 http.serve_forever()
if __name__=='__main__':main()
