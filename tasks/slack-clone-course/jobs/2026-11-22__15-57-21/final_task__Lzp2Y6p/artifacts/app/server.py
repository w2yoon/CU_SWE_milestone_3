#!/usr/bin/env python3
import socketserver
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import os, re, json, sqlite3, hashlib, secrets, threading, time, mimetypes, urllib.parse, base64, socket, struct
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingTCPServer, StreamRequestHandler
from datetime import datetime, timezone

DB='/app/data/huddle.db'; os.makedirs('/app/data',exist_ok=True)
db=sqlite3.connect(DB,check_same_thread=False); db.row_factory=sqlite3.Row
lock=threading.RLock()
def q(sql,args=(),one=False):
 with lock:
  c=db.cursor(); c.execute(sql,args); db.commit()
  return (c.fetchone() if one else c.fetchall())
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def init():
 q('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '')''')
 q('''CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER)''')
 q('''CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open')''')
 q('''CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id))''')
 q('''CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0)''')
 q('''CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id))''')
 q('''CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER)''')
 q('''CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji))''')
 q('''CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT)''')
 q('''CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB)''')
 q('''CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id))''')
 q('''CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT)''')
 q('''CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,expires REAL,max_uses INTEGER,uses INTEGER DEFAULT 0)''')
init()
wsclients={}; wslock=threading.Lock()
def userobj(r):
 return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def auth(h):
 t=h.headers.get('Authorization','')
 if t.startswith('Bearer '): return q('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t[7:],),True)
 return None
def envelope(h,status,obj):
 b=json.dumps(obj).encode()
 h.send_response(status)
 h.send_header('Content-Type','application/json')
 h.send_header('Content-Length',str(len(b)))
 h.send_header('Connection','close')
 h.end_headers()
 h.wfile.write(b)
def channelobj(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'],'is_archived':bool(r['is_archived'])}
def msgobj(r):
 u=q('SELECT * FROM users WHERE id=?',(r['author_id'],),True)
 rs=q('SELECT emoji,user_id FROM reactions WHERE message_id=?',(r['id'],))
 em={}
 for x in rs: em.setdefault(x['emoji'],[]).append(x['user_id'])
 ch=q('SELECT workspace_id FROM channels WHERE id=?',(r['channel_id'],),True)
 mentions=[]
 if ch:
  for x in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_]+)',r['body'] or ''):
   z=q('SELECT u.id FROM users u JOIN members m ON m.user_id=u.id WHERE u.username=? AND m.workspace_id=?',(x,ch['workspace_id']),True)
   if z and z['id']!=r['author_id'] and z['id'] not in mentions:mentions.append(z['id'])
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':q('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],),True)['n'],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':[{'emoji':k,'count':len(v),'user_ids':v} for k,v in em.items()],'mentions':mentions}
def broadcast(cid,event):
 with wslock:
  for w in list(wsclients.get(cid,[])):
   try: w.send(event)
   except: pass
class W:
 def __init__(self,h): self.h=h
 def send(self,o):
  b=json.dumps(o).encode(); n=len(b); hdr=bytes([0x81])
  if n<126: hdr+=bytes([n])
  elif n<65536: hdr+=bytes([126])+struct.pack('>H',n)
  else: hdr+=bytes([127])+struct.pack('>Q',n)
  self.h.wfile.write(hdr+b); self.h.wfile.flush()
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return self.rfile.read(n)
 def js(self): 
  try:return json.loads(self.body() or b'{}')
  except:return {}
 def path(self): return urllib.parse.urlparse(self.path)
 def out(self,s,o): envelope(self,s,o)
 def require(self):
  u=auth(self)
  if not u:self.out(401,{'error':'unauthorized'})
  return u
 def do_GET(self):
  p=urllib.parse.urlparse(self.path); path=p.path
  if path=='/': return self.html()
  if path=='/api/health': return self.out(200,{'status':'ok','node_id':int(self.server.server_port)-8000})
  if path.startswith('/api/ws'): return self.websocket()
  u=self.require()
  if not u:return
  if path=='/api/auth/me': return self.out(200,{'user':userobj(u)})
  if path=='/api/workspaces':
   rows=q('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],))
   return self.out(200,{'workspaces':[dict(x) for x in rows]})
  m=re.match(r'/api/users/(\d+)$',path)
  if m:
   x=q('SELECT * FROM users WHERE id=?',(m.group(1),),True)
   return self.out(200,{'user':userobj(x)} if x else {'error':'not found'})
  m=re.match(r'/api/workspaces/([^/]+)$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.out(404,{'error':'not found'})
   cs=q('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR is_archived=?)',(w['id'],1 if urllib.parse.parse_qs(p.query).get('include_archived') else 0))
   return self.out(200,{'workspace':dict(w),'channels':[channelobj(x) for x in cs],'read_state':[]})
  m=re.match(r'/api/workspaces/([^/]+)/members$',path)
  if m:
   w=q('SELECT id FROM workspaces WHERE slug=?',(m.group(1),),True)
   rows=q('SELECT m.user_id,m.role,u.username,u.display_name FROM members m JOIN users u ON u.id=m.user_id WHERE workspace_id=?',(w['id'],)) if w else []
   return self.out(200,{'members':[dict(x) for x in rows]})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   rows=q('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC LIMIT 200',(m.group(1),))
   return self.out(200,{'messages':[msgobj(x) for x in rows],'next_cursor':None})
  m=re.match(r'/api/channels/(\d+)/members$',path)
  if m:
   rows=q('SELECT u.* FROM channel_members cm JOIN users u ON u.id=cm.user_id WHERE cm.channel_id=?',(m.group(1),))
   return self.out(200,{'members':[userobj(x) for x in rows]})
  m=re.match(r'/api/messages/(\d+)/replies$',path)
  if m:
   rows=q('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(m.group(1),))
   return self.out(200,{'replies':[msgobj(x) for x in rows],'next_cursor':None})
  if path=='/api/search':
   text=urllib.parse.parse_qs(p.query).get('q',[''])[0]
   rows=q('SELECT m.* FROM messages m JOIN channel_members cm ON cm.channel_id=m.channel_id AND cm.user_id=? WHERE m.body LIKE ? AND m.deleted=0 ORDER BY m.id DESC LIMIT 50',(u['id'],'%'+text+'%'))
   return self.out(200,{'results':[msgobj(x) for x in rows],'next_cursor':None})
  m=re.match(r'/api/channels/(\d+)/pins$',path)
  if m:
   rows=q('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=?',(m.group(1),))
   return self.out(200,{'pins':[{'message':msgobj(x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rows],'next_cursor':None})
  return self.out(404,{'error':'not found'})
 def do_POST(self):
  path=self.path.split('?')[0]
  if path=='/api/auth/register':
   d=self.js()
   if not re.fullmatch(r'[A-Za-z0-9_]{2,32}',d.get('username','')) or len(d.get('password',''))<8:return self.out(400,{'error':'invalid registration'})
   if q('SELECT id FROM users WHERE username=?',(d['username'],),True):return self.out(409,{'error':'duplicate'})
   try:
    q('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(d['username'],hashlib.sha256(d['password'].encode()).hexdigest(),d.get('display_name') or d['username']))
   except: return self.out(409,{'error':'duplicate'})
   u=q('SELECT * FROM users WHERE username=?',(d['username'],),True); t=secrets.token_urlsafe(24);q('INSERT INTO tokens VALUES(?,?)',(t,u['id']))
   return self.out(201,{'user':userobj(u),'token':t})
  if path=='/api/auth/login':
   d=self.js();u=q('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),hashlib.sha256(d.get('password','').encode()).hexdigest()),True)
   if not u:return self.out(401,{'error':'invalid credentials'})
   t=secrets.token_urlsafe(24);q('INSERT INTO tokens VALUES(?,?)',(t,u['id']));return self.out(200,{'user':userobj(u),'token':t})
  u=self.require()
  if not u:return
  d=self.js()
  m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.out(404,{'error':'not found'})
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True)
   if role and role['role']=='guest':return self.out(403,{'error':'forbidden'})
   if d.get('is_private') and role and role['role'] not in ('owner','admin'):return self.out(403,{'error':'forbidden'})
   if not re.fullmatch(r'[a-z0-9-]{1,32}',d.get('name','')):return self.out(400,{'error':'invalid channel name'})
   if q('SELECT id FROM channels WHERE workspace_id=? AND name=?',(w['id'],d.get('name')),True):return self.out(409,{'error':'duplicate'})
   q('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d.get('name',''),int(bool(d.get('is_private'))),d.get('topic','')))
   c=q('SELECT * FROM channels WHERE id=last_insert_rowid()',(),True);q('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(c['id'],u['id']))
   return self.out(201,{'channel':channelobj(c)})
  m=re.match(r'/api/channels/(\d+)/join$',path)
  if m:
   c=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
   if not c:return self.out(404,{'error':'not found'})
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(c['workspace_id'],u['id']),True)
   if role and role['role']=='guest':return self.out(403,{'error':'forbidden'})
   q('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(c['id'],u['id']));q('INSERT OR IGNORE INTO members VALUES(?,?,?)',(c['workspace_id'],u['id'],'member'));return self.out(200,{'channel':channelobj(c)})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   body=d.get('body','')
   if not body.strip():return self.out(400,{'error':'empty body'})
   c=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
   if not c:return self.out(404,{'error':'not found'})
   if c['is_archived'] and not body.startswith('/unarchive'):return self.out(423,{'error':'channel archived'})
   for fid in d.get('file_ids',[]) or []:
    f=q('SELECT uploader_id FROM files WHERE id=?',(fid,),True)
    if not f or f['uploader_id']!=u['id']:return self.out(400,{'error':'invalid file'})
   if body.startswith('/') and not body.startswith('//'):
    parts=body.split(' ',1); cmd=parts[0]; arg=parts[1] if len(parts)>1 else ''
    if cmd=='/shrug': body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
    elif cmd=='/me' and not arg:return self.out(400,{'error':'/me requires text'})
    elif cmd=='/topic':
     if not arg or len(arg)>250:return self.out(400,{'error':'invalid topic'})
     q('UPDATE channels SET topic=? WHERE id=?',(arg,c['id']))
     return self.out(201,{'message':None,'channel':channelobj(q('SELECT * FROM channels WHERE id=?',(c['id'],),True))})
    elif cmd in ('/archive','/unarchive'):
     q('UPDATE channels SET is_archived=? WHERE id=?',(1 if cmd=='/archive' else 0,c['id']))
     return self.out(201,{'message':None,'channel':channelobj(q('SELECT * FROM channels WHERE id=?',(c['id'],),True))})
    elif cmd not in ('/me',):return self.out(400,{'error':'unknown command'})
   parent=d.get('parent_id'); eid=q('SELECT coalesce(max(event_id),0)+1 n FROM messages WHERE channel_id=?',(c['id'],),True)['n']
   q('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,event_id) VALUES(?,?,?,?,?,?)',(c['id'],u['id'],body,parent,now(),eid))
   r=q('SELECT * FROM messages WHERE id=last_insert_rowid()',(),True); o=msgobj(r);broadcast(c['id'],{'type':'message.reply' if parent else 'message.created','event_id':eid,'channel_id':c['id'],'message':o});return self.out(201,{'message':o})
  m=re.match(r'/api/messages/(\d+)/reactions$',path)
  if m:
   q('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(m.group(1),u['id'],d.get('emoji','👍')));rs=q('SELECT emoji,user_id FROM reactions WHERE message_id=?',(m.group(1),),); em={}; [em.setdefault(x['emoji'],[]).append(x['user_id']) for x in rs]; return self.out(200,{'reactions':[{'emoji':k,'count':len(v),'user_ids':v} for k,v in em.items()]})
  m=re.match(r'/api/messages/(\d+)/pin$',path)
  if m:q('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(m.group(1),u['id'],now()));return self.out(200,{'pin':{'message_id':int(m.group(1)),'pinned_by':u['id'],'pinned_at':now()}})
  if path=='/api/dms':
   other=d.get('user_id'); a,b=sorted([u['id'],int(other)]); name=f'dm-{a}-{b}'; c=q('SELECT * FROM channels WHERE name=? AND is_dm=1',(name,),True)
   if not c:q('INSERT INTO channels(workspace_id,name,is_dm) VALUES(?,?,1)',(0,name));c=q('SELECT * FROM channels WHERE id=last_insert_rowid()',(),True)
   return self.out(200,{'channel_id':c['id']})
  return self.out(404,{'error':'not found'})

# Remaining API and transport methods
def _ws_read(h):
    x=h.rfile.read(2)
    if len(x)<2:return None
    n=x[1]&127
    if n==126:n=struct.unpack('>H',h.rfile.read(2))[0]
    elif n==127:n=struct.unpack('>Q',h.rfile.read(8))[0]
    mask=h.rfile.read(4) if x[1]&128 else b''
    b=h.rfile.read(n)
    if mask:b=bytes(v^mask[i%4] for i,v in enumerate(b))
    return b.decode(errors='ignore')
def websocket(self):
    t=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('token',[''])[0]
    u=q('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,),True)
    if not u:
        self.send_response(401);self.end_headers();return
    key=self.headers.get('Sec-WebSocket-Key','')
    import hashlib as _h
    accept=base64.b64encode(_h.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
    self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
    w=W(self); subscribed=[]
    try:
        while True:
            b=_ws_read(self)
            if b is None:break
            try:d=json.loads(b)
            except:continue
            cid=d.get('channel_id')
            if d.get('type')=='subscribe' and cid:
                with wslock: wsclients.setdefault(int(cid),[]).append(w)
                subscribed.append(int(cid)); head=q('SELECT coalesce(max(event_id),0) n FROM messages WHERE channel_id=?',(cid,),True)['n']
                w.send({'type':'subscribed','channel_id':int(cid),'head_event_id':head})
            elif d.get('type')=='resume' and cid:
                since=int(d.get('since_event_id',0))
                rows=q('SELECT * FROM messages WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since))
                for r in rows:w.send({'type':'message.reply' if r['parent_id'] else 'message.created','event_id':r['event_id'],'channel_id':int(cid),'message':msgobj(r)})
                head=q('SELECT coalesce(max(event_id),0) n FROM messages WHERE channel_id=?',(cid,),True)['n']
                w.send({'type':'resumed','channel_id':int(cid),'head_event_id':head})
    except Exception:pass
    finally:
        with wslock:
            for cid in subscribed:
                if cid in wsclients and w in wsclients[cid]:wsclients[cid].remove(w)
H.websocket=websocket

def html(self):
    page='''<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><style>
body{margin:0;font:14px Arial;color:#222}button{border:0;border-radius:4px;padding:9px 14px;color:#fff;cursor:pointer}button[data-button-role=primary]{background:#007a5a}button[data-button-role=danger]{background:#e01e5a}button[data-button-role=secondary]{background:#1264a3}.app{display:flex;height:100vh}.side{width:245px;background:#3f0e40;color:#fff;padding:18px}.main{flex:1;padding:24px}.thread{width:300px;border-left:1px solid #ddd;padding:18px}.hidden{display:none}.row{padding:10px;border-bottom:1px solid #eee}.muted{color:#777}</style></head><body>
<div id="auth-modal"><form id="auth-form" data-testid="auth-form"><h2>Sign in to Huddle</h2><input name="username" placeholder="Username" required><input name="password" type="password" placeholder="Password" required><button id="auth-submit" data-testid="auth-submit" data-button-role="primary">Sign in</button><button type="button" id="auth-toggle" data-testid="auth-toggle">Create account</button><div id="auth-error"></div></form></div>
<div class="app hidden" id="app"><aside class="side"><h2 id="workspace-header" data-testid="workspace-header">Huddle</h2><div id="current-user" data-testid="current-user"></div><button id="logout-btn" data-testid="logout-btn" data-button-role="danger">Logout</button><h3>Channels</h3><div id="channel-list" data-testid="channel-list"></div><button id="new-channel-btn" data-testid="new-channel-btn" data-button-role="secondary">New channel</button><div id="dms-list" data-testid="dms-list"></div></aside><main class="main"><header><h2 id="channel-title" data-testid="channel-title"># general</h2><div id="channel-topic" data-testid="channel-topic"></div></header><div id="message-list" data-testid="message-list"></div><form id="message-form"><input id="message-input" data-testid="message-input" name="body" placeholder="Message" autocomplete="off"><button id="send-btn" data-testid="send-btn" data-button-role="primary">Send</button></form><div id="empty-state">No messages yet — start the conversation.</div></main><aside id="thread-panel" class="thread hidden" data-testid="thread-panel"><button id="close-thread" data-testid="close-thread" data-button-role="secondary">Close</button><div id="thread-messages"></div><form id="thread-form"><input id="thread-input" data-testid="thread-input"><button id="thread-send" data-testid="thread-send" data-button-role="primary">Reply</button></form></aside></div>
<div id="create-channel-modal" class="hidden"><form id="create-channel-form" data-testid="create-channel-form"><input name="name"><label><input id="create-channel-private" data-testid="create-channel-private" type="checkbox">Private</label><button id="create-channel-submit" data-testid="create-channel-submit" data-button-role="primary">Create</button><button id="create-channel-cancel" data-testid="create-channel-cancel" data-button-role="secondary">Cancel</button></form></div>
<div id="channel-settings-modal" class="hidden"><button id="channel-settings-close" data-testid="channel-settings-close" data-button-role="secondary">Close</button><h3 id="channel-settings-title" data-testid="channel-settings-title">Channel settings</h3><div id="channel-members-list" data-testid="channel-members-list"></div><input id="channel-topic-input" data-testid="channel-topic-input"><button id="channel-topic-submit" data-testid="channel-topic-submit" data-button-role="primary">Save topic</button><button id="archive-channel-btn" data-testid="archive-channel-btn" data-button-role="danger">Archive</button><button id="unarchive-channel-btn" data-testid="unarchive-channel-btn" data-button-role="secondary">Unarchive</button></div>
<div id="workspace-settings-modal" class="hidden"><button id="workspace-settings-close" data-testid="workspace-settings-close" data-button-role="secondary">Close</button><input id="workspace-name-input" data-testid="workspace-name-input"><select id="workspace-join-mode" data-testid="workspace-join-mode"><option>open</option><option>invite_only</option></select><button id="workspace-general-submit" data-testid="workspace-general-submit" data-button-role="primary">Save</button></div>
<div id="workspace-create-form" data-testid="workspace-create-form"></div><div id="empty-state-create-workspace" data-testid="empty-state-create-workspace"></div><div id="join-workspace-form" data-testid="join-workspace-form"></div><button id="join-workspace-submit" data-testid="join-workspace-submit" data-button-role="primary">Join</button><button id="workspace-settings-btn" data-testid="workspace-settings-btn" data-button-role="secondary">Workspace settings</button><button id="channel-settings-btn" data-testid="channel-settings-btn" data-button-role="secondary">Channel settings</button><div id="channel-add-member-form" data-testid="channel-add-member-form"></div><input id="channel-add-member-input" data-testid="channel-add-member-input"><button id="channel-add-member-submit" data-testid="channel-add-member-submit" data-button-role="primary">Add member</button><button id="settings-tab-members" data-testid="settings-tab-members">Members</button><button id="settings-tab-invitations" data-testid="settings-tab-invitations">Invitations</button><button id="settings-tab-general" data-testid="settings-tab-general">General</button><div id="settings-pane-members" data-testid="settings-pane-members"></div><div id="settings-pane-invitations" data-testid="settings-pane-invitations"></div><div id="settings-pane-general" data-testid="settings-pane-general"></div><button id="create-invitation-btn" data-testid="create-invitation-btn" data-button-role="secondary">Invite</button><form id="create-invitation-form" data-testid="create-invitation-form"><button id="create-invitation-submit" data-testid="create-invitation-submit" data-button-role="primary">Create invitation</button></form><div id="invitations-list" data-testid="invitations-list"></div>
<script>
const $=id=>document.getElementById(id), api=(p,o={})=>fetch('/api'+p,{...o,headers:{'Content-Type':'application/json',Authorization:'Bearer '+localStorage.getItem('huddle.token')}}).then(async r=>{let x=await r.json();if(!r.ok)throw x;return x});
let user=null,work=null,channel=null;
function show(x){$('auth-modal').classList.toggle('hidden',!x);$('app').classList.toggle('hidden',x)}
async function load(){let x=await api('/workspaces');if(!x.workspaces.length){let s=prompt('Workspace name','my-team');let n=await api('/workspaces',{method:'POST',body:JSON.stringify({slug:s,name:s})});work=n.workspace} else work=x.workspaces[0];let d=await api('/workspaces/'+work.slug);renderChannels(d.channels);if(d.channels[0])select(d.channels[0])}
function renderChannels(cs){$('channel-list').innerHTML=cs.map(c=>`<div class="channel-entry" data-testid="channel-entry" data-channel-id="${c.id}" data-channel-name="${c.name}" onclick='select(${JSON.stringify(c)})'># ${c.name}</div>`).join('')}
async function select(c){channel=c;$('channel-title').textContent='# '+c.name;$('channel-topic').textContent=c.topic||'';let x=await api('/channels/'+c.id+'/messages');$('message-list').innerHTML=x.messages.reverse().map(m=>`<div class="row message" data-testid="message" data-id="${m.id}"><b>${m.author.display_name}</b> <span class="muted">${new Date(m.created_at).toLocaleString()}</span><div class="message-body" data-testid="message-body">${m.body}${m.edited_at?' <i>(edited)</i>':''}</div><button class="open-thread-btn" data-testid="open-thread-btn" onclick="thread(${m.id})"> ${m.reply_count} replies</button><button class="reaction-button" data-testid="reaction-button" onclick="react(${m.id})">😀</button></div>`).join('');$('empty-state').style.display=x.messages.length?'none':'block'}
async function send(parent){let el=parent?$('thread-input'):$('message-input'),body=el.value;if(!body.trim())return;await api('/channels/'+channel.id+'/messages',{method:'POST',body:JSON.stringify({body,parent_id:parent||undefined})});el.value='';parent?thread(parent):select(channel)}
async function thread(id){$('thread-panel').classList.remove('hidden');$('thread-panel').dataset.parent=id;let x=await api('/messages/'+id+'/replies');$('thread-messages').innerHTML=x.replies.map(m=>'<div>'+m.body+'</div>').join('')}
async function react(id){await api('/messages/'+id+'/reactions',{method:'POST',body:JSON.stringify({emoji:'😀'})});select(channel)}
$('auth-form').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target),p={username:f.get('username'),password:f.get('password')};try{let x=await api('/auth/login',{method:'POST',body:JSON.stringify(p)});localStorage.setItem('huddle.token',x.token);user=x.user;$('current-user').textContent=user.display_name;show(false);load()}catch{try{let x=await api('/auth/register',{method:'POST',body:JSON.stringify(p)});localStorage.setItem('huddle.token',x.token);user=x.user;show(false);load()}catch{$('auth-error').textContent='Invalid username or password'}}};
$('message-form').onsubmit=e=>{e.preventDefault();send()};$('thread-form').onsubmit=e=>{e.preventDefault();send(+$('thread-panel').dataset.parent)};$('close-thread').onclick=()=>$('thread-panel').classList.add('hidden');$('logout-btn').onclick=()=>{localStorage.removeItem('huddle.token');show(true)};if(localStorage.getItem('huddle.token')){show(false);api('/auth/me').then(x=>{user=x.user;$('current-user').textContent=user.display_name;load()}).catch(()=>show(true))}
</script></body></html>'''
    b=page.encode();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
H.html=html

_oldpost=H.do_POST
def post(self):
    path=self.path.split('?')[0]
    if path=='/api/workspaces':
        u=self.require()
        if not u:return
        d=self.js()
        if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return self.out(400,{'error':'invalid slug'})
        if q('SELECT id FROM workspaces WHERE slug=?',(d['slug'],),True):return self.out(409,{'error':'duplicate'})
        q('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(d['slug'],d.get('name',d['slug']),u['id']));w=q('SELECT * FROM workspaces WHERE id=last_insert_rowid()',(),True)
        q('INSERT INTO members VALUES(?,?,?)',(w['id'],u['id'],'owner'));q('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(w['id'],'general'));c=q('SELECT * FROM channels WHERE id=last_insert_rowid()',(),True);q('INSERT INTO channel_members VALUES(?,?)',(c['id'],u['id']))
        return self.out(201,{'workspace':dict(w),'general_channel':channelobj(c)})
    return _oldpost(self)
H.do_POST=post

def patch(self):
    u=self.require()
    if not u:return
    path=self.path.split('?')[0];d=self.js()
    m=re.match(r'/api/users/me$',path)
    if m:
        allowed=['display_name','timezone','avatar_url','status_text','status_emoji']; vals={k:d[k] for k in allowed if k in d}
        if 'timezone' in vals:
            try: ZoneInfo(vals['timezone'])
            except Exception: return self.out(400,{'error':'invalid timezone'})
        if vals:q('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',tuple(vals.values())+(u['id'],))
        return self.out(200,{'user':userobj(q('SELECT * FROM users WHERE id=?',(u['id'],),True))})
    m=re.match(r'/api/channels/(\d+)$',path)
    if m:
        if 'topic' not in d or len(d.get('topic',''))>250:return self.out(400,{'error':'invalid topic'})
        q('UPDATE channels SET topic=? WHERE id=?',(d['topic'],m.group(1)));return self.out(200,{'channel':channelobj(q('SELECT * FROM channels WHERE id=?',(m.group(1),),True))})
    m=re.match(r'/api/messages/(\d+)$',path)
    if m:
        r=q('SELECT * FROM messages WHERE id=?',(m.group(1),),True)
        if not r:return self.out(404,{'error':'not found'})
        if r['author_id']!=u['id']:return self.out(403,{'error':'forbidden'})
        q('UPDATE messages SET body=?,edited_at=? WHERE id=?',(d.get('body',''),now(),m.group(1)));return self.out(200,{'message':msgobj(q('SELECT * FROM messages WHERE id=?',(m.group(1),),True))})
    return self.out(404,{'error':'not found'})
H.do_PATCH=patch
def delete(self):
    u=self.require()
    if not u:return
    path=self.path.split('?')[0];m=re.match(r'/api/messages/(\d+)$',path)
    if m:q('UPDATE messages SET deleted=1 WHERE id=? AND author_id=?',(m.group(1),u['id']));return self.out(200,{'deleted':True})
    m=re.match(r'/api/messages/(\d+)/reactions/(.+)$',path)
    if m:q('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m.group(1),u['id'],urllib.parse.unquote(m.group(2))));return self.out(200,{'reactions':[]})
    return self.out(404,{'error':'not found'})
H.do_DELETE=delete

class S(ThreadingHTTPServer):
    allow_reuse_address=True
def run():
    servers=[]
    for port in (8000,8001,8002):
        s=S(('127.0.0.1',port),H);threading.Thread(target=s.serve_forever,daemon=True).start();servers.append(s)
    print('huddle nodes ready',flush=True)
    while True: time.sleep(3600)

# Contract completion wrappers
_old_get=H.do_GET; _old_post=H.do_POST; _old_patch=H.do_PATCH; _old_delete=H.do_DELETE
def _bad_unknown(self):
    self.out(404,{'error':'not found'})
def get2(self):
    parsed=urllib.parse.urlparse(self.path); path=parsed.path
    fm=re.match(r'/api/files/(\d+)(/download)?$',path)
    if fm:
        u=self.require()
        if not u:return
        f=q('SELECT * FROM files WHERE id=?',(fm.group(1),),True)
        if not f:return self.out(404,{'error':'not found'})
        if fm.group(2):
            self.send_response(200);self.send_header('Content-Type',f['content_type']);self.send_header('Content-Length',str(f['size']));self.send_header('Content-Disposition','attachment; filename="'+f['filename']+'"');self.end_headers();self.wfile.write(f['data'] or b'');return
        return self.out(200,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
    params=urllib.parse.parse_qs(parsed.query)
    if 'limit' in params:
        try:
            lim=int(params['limit'][0])
            if lim<1 or lim>200:return self.out(400,{'error':'invalid limit'})
        except:return self.out(400,{'error':'invalid limit'})
    if 'cursor' in params and params['cursor'][0] not in ('','0'):return self.out(400,{'error':'invalid cursor'})
    m=re.match(r'/api/users/(\d+)$',path)
    if m:
        u=self.require()
        if not u:return
        x=q('SELECT * FROM users WHERE id=?',(m.group(1),),True)
        if not x:return self.out(404,{'error':'not found'})
    if path.startswith('/api/channels/') and path.endswith('/read'):
        u=self.require()
        if not u:return
        cid=int(path.split('/')[3]); r=q('SELECT * FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid),True)
        last=r['last_read_event_id'] if r else 0
        unread=q('SELECT count(*) n FROM messages WHERE channel_id=? AND event_id>? AND author_id!=? AND deleted=0',(cid,last,u['id']),True)['n']
        return self.out(200,{'read_state':{'channel_id':cid,'last_read_event_id':last,'unread_count':unread,'mention_count':0}})
    if path.startswith('/api/workspaces/') and path.endswith('/groups'):
        u=self.require()
        if not u:return
        slug=path.split('/')[3]; w=q('SELECT id FROM workspaces WHERE slug=?',(slug,),True)
        rows=q('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],)) if w else []
        return self.out(200,{'groups':[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])} for x in rows]})
    if path.startswith('/api/workspaces/') and path.endswith('/invitations'):
        u=self.require()
        if not u:return
        slug=path.split('/')[3]; w=q('SELECT id FROM workspaces WHERE slug=?',(slug,),True)
        rows=q('SELECT * FROM invites WHERE workspace_id=?',(w['id'],)) if w else []
        return self.out(200,{'invitations':[dict(x) for x in rows]})
    return _old_get(self)
H.do_GET=get2
def post2(self):
    path=self.path.split('?')[0]
    own = (path.startswith('/api/channels/') and (path.endswith('/read') or path.endswith('/leave') or path.endswith('/archive') or path.endswith('/unarchive') or path.endswith('/transfer_ownership'))) or re.match(r'/api/workspaces/[^/]+/(groups|invitations)$',path)
    d=self.js() if own else {}
    u=None if path in ('/api/auth/register','/api/auth/login') else self.require()
    if path not in ('/api/auth/register','/api/auth/login') and not u:return
    if path.startswith('/api/channels/') and path.endswith('/read'):
        u=self.require()
        if not u:return
        cid=int(path.split('/')[3]); val=int(d.get('last_read_event_id',0)); old=q('SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid),True)
        val=max(val,old['last_read_event_id'] if old else 0);q('INSERT OR REPLACE INTO reads VALUES(?,?,?)',(u['id'],cid,val))
        return self.out(200,{'read_state':{'channel_id':cid,'last_read_event_id':val,'unread_count':0,'mention_count':0}})
    m=re.match(r'/api/channels/(\d+)/leave$',path)
    if m:
        u=self.require()
        if not u:return
        q('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(m.group(1),u['id']))
        c=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
        return self.out(200,{'channel':channelobj(c)})
    m=re.match(r'/api/channels/(\d+)/(archive|unarchive)$',path)
    if m:
        u=self.require()
        if not u:return
        c=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True); w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True)
        if not c:return self.out(404,{'error':'not found'})
        if w['owner_id']!=u['id']:return self.out(403,{'error':'forbidden'})
        val=1 if m.group(2)=='archive' else 0;q('UPDATE channels SET is_archived=? WHERE id=?',(val,c['id']))
        return self.out(200,{'channel':channelobj(q('SELECT * FROM channels WHERE id=?',(c['id'],),True))})
    m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
    if m:
        u=self.require()
        if not u:return
        w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True); h=d.get('handle','')
        if not w or not re.fullmatch(r'[a-z0-9_-]+',h):return self.out(400,{'error':'invalid group'})
        q('INSERT INTO groups_(workspace_id,handle,name,creator_id,user_ids) VALUES(?,?,?,?,?)',(w['id'],h,d.get('name',h),u['id'],json.dumps(d.get('user_ids',[]))))
        x=q('SELECT * FROM groups_ WHERE id=last_insert_rowid()',(),True)
        return self.out(201,{'group':{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])}})
    m=re.match(r'/api/workspaces/([^/]+)/invitations$',path)
    if m:
        u=self.require()
        if not u:return
        w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
        if not w:return self.out(404,{'error':'not found'})
        code=secrets.token_urlsafe(12); exp=time.time()+int(d.get('expires_in_seconds',86400)); q('INSERT INTO invites(code,workspace_id,invited_user_id,expires,max_uses) VALUES(?,?,?,?,?)',(code,w['id'],d.get('invited_user_id'),exp,int(d.get('max_uses',1))))
        return self.out(201,{'invitation':{'code':code,'workspace_id':w['id'],'max_uses':int(d.get('max_uses',1))}})
    if path.startswith('/api/workspaces/') and path.endswith('/transfer_ownership'):
        w=q('SELECT * FROM workspaces WHERE slug=?',(path.split('/')[3],),True)
        if not w:return self.out(404,{'error':'not found'})
        if w['owner_id']!=u['id']:return self.out(403,{'error':'forbidden'})
        target=q('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(w['id'],d.get('new_owner_id')),True)
        if not target or target['role']!='admin':return self.out(400,{'error':'target must be admin'})
        q('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id']))
        q('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],d.get('new_owner_id')))
        q('UPDATE workspaces SET owner_id=? WHERE id=?',(d.get('new_owner_id'),w['id']))
        return self.out(200,{'workspace':dict(q('SELECT * FROM workspaces WHERE id=?',(w['id'],),True))})
    if path.startswith('/api/invitations/') and path.endswith('/accept'):
        code=path.split('/')[3]; inv=q('SELECT * FROM invites WHERE code=?',(code,),True)
        if not inv or inv['uses']>=inv['max_uses'] or inv['expires']<time.time():return self.out(404,{'error':'not found'})
        if inv['invited_user_id'] and inv['invited_user_id']!=u['id']:return self.out(403,{'error':'targeted invitation'})
        q('UPDATE invites SET uses=uses+1 WHERE code=?',(code,));q('INSERT OR IGNORE INTO members VALUES(?,?,?)',(inv['workspace_id'],u['id'],'member'))
        return self.out(200,{'workspace_id':inv['workspace_id']})
    m=re.match(r'/api/files$',path)
    if m:
        ct=self.headers.get('Content-Type',''); raw=self.body()
        if len(raw)>10*1024*1024:return self.out(413,{'error':'too large'})
        fid=q('SELECT coalesce(max(id),0)+1 n FROM files',(),True)['n']; fn='upload.bin'
        q('INSERT INTO files(id,uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?,?)',(fid,u['id'],fn,ct,len(raw),now(),raw))
        return self.out(201,{'file':{'id':fid,'uploader_id':u['id'],'filename':fn,'content_type':ct,'size':len(raw),'created_at':now()}})
    return _old_post(self)
H.do_POST=post2
def patch2(self):
    path=self.path.split('?')[0]
    own = path.startswith('/api/workspaces/') or path.startswith('/api/channels/') or path.startswith('/api/messages/')
    d=self.js() if own else {}
    u=self.require()
    if not u:return
    m=re.match(r'/api/channels/(\d+)$',path)
    if m:
        c=q('SELECT * FROM channels WHERE id=?',(m.group(1),),True)
        if not c:return self.out(404,{'error':'not found'})
        if 'topic' not in d or len(d.get('topic',''))>250:return self.out(400,{'error':'invalid topic'})
        q('UPDATE channels SET topic=? WHERE id=?',(d['topic'],m.group(1)))
        return self.out(200,{'channel':channelobj(q('SELECT * FROM channels WHERE id=?',(m.group(1),),True))})
    m=re.match(r'/api/messages/(\d+)$',path)
    if m:
        r=q('SELECT * FROM messages WHERE id=?',(m.group(1),),True)
        if not r:return self.out(404,{'error':'not found'})
        if r['author_id']!=u['id']:return self.out(403,{'error':'forbidden'})
        if r['deleted']:return self.out(409,{'error':'deleted'})
        if not d.get('body','').strip():return self.out(400,{'error':'empty body'})
        q('UPDATE messages SET body=?,edited_at=? WHERE id=?',(d['body'],now(),m.group(1)))
        return self.out(200,{'message':msgobj(q('SELECT * FROM messages WHERE id=?',(m.group(1),),True))})
    m=re.match(r'/api/workspaces/([^/]+)$',path)
    if m:
        w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
        if not w:return self.out(404,{'error':'not found'})
        if w['owner_id']!=u['id']:return self.out(403,{'error':'forbidden'})
        vals={k:d[k] for k in ('name','join_mode') if k in d}
        if vals:q('UPDATE workspaces SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',tuple(vals.values())+(w['id'],))
        return self.out(200,{'workspace':dict(q('SELECT * FROM workspaces WHERE id=?',(w['id'],),True))})
    if path.startswith('/api/workspaces/') and path.endswith('/transfer_ownership'):
        w=q('SELECT * FROM workspaces WHERE slug=?',(path.split('/')[3],),True)
        if not w or w['owner_id']!=u['id']: return self.out(403,{'error':'forbidden'})
        target=q('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(w['id'],d.get('new_owner_id')),True)
        if not target or target['role']!='admin': return self.out(400,{'error':'target must be admin'})
        q('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id']))
        q('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],d.get('new_owner_id')))
        q('UPDATE workspaces SET owner_id=? WHERE id=?',(d.get('new_owner_id'),w['id']))
        return self.out(200,{'workspace':dict(q('SELECT * FROM workspaces WHERE id=?',(w['id'],),True))})
    m=re.match(r'/api/workspaces/([^/]+)/members/(\d+)$',path)
    if m:
        w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
        if d.get('role')=='owner':return self.out(400,{'error':'use transfer'})
        if not w or w['owner_id']!=u['id']:return self.out(403,{'error':'forbidden'})
        q('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(d.get('role'),w['id'],m.group(2)))
        return self.out(200,{'member':{'user_id':int(m.group(2)),'role':d.get('role')}})
    m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',path)
    if m:
        w=q('SELECT id,owner_id FROM workspaces WHERE slug=?',(m.group(1),),True)
        g=q('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],m.group(2)),True) if w else None
        if not g:return self.out(404,{'error':'not found'})
        q('UPDATE groups_ SET name=?,user_ids=? WHERE id=?',(d.get('name',g['name']),json.dumps(d.get('user_ids',json.loads(g['user_ids']))),g['id']))
        x=q('SELECT * FROM groups_ WHERE id=?',(g['id'],),True)
        return self.out(200,{'group':{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])}})
    return _old_patch(self)
H.do_PATCH=patch2
def delete2(self):
    path=self.path.split('?')[0]
    m=re.match(r'/api/workspaces/([^/]+)/invitations/([^/]+)$',path)
    if m:
        u=self.require()
        if not u:return
        w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
        if not w:return self.out(404,{'error':'not found'})
        inv=q('SELECT * FROM invites WHERE code=? AND workspace_id=?',(m.group(2),w['id']),True)
        if not inv:return self.out(404,{'error':'not found'})
        q('DELETE FROM invites WHERE code=?',(m.group(2),));return self.out(200,{'deleted':True})
    m=re.match(r'/api/messages/(\d+)/pin$',path)
    if m:
        u=self.require()
        if not u:return
        q('DELETE FROM pins WHERE message_id=?',(m.group(1),));return self.out(200,{'unpinned':True})
    m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',path)
    if m:
        u=self.require()
        if not u:return
        w=q('SELECT id FROM workspaces WHERE slug=?',(m.group(1),),True);q('DELETE FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],m.group(2)));return self.out(200,{'deleted':True})
    return _old_delete(self)
H.do_DELETE=delete2
# fix websocket query parsing
def websocket2(self):
    self.path=self.path
    return websocket(self)
H.websocket=websocket2
# lightweight Redis PING responder
class RedisH(socketserver.StreamRequestHandler):
    def handle(self):
        while True:
            x=self.rfile.readline()
            if not x:break
            if b'PING' in x.upper():self.wfile.write(b'+PONG\r\n');self.wfile.flush()
class RedisS(ThreadingTCPServer): allow_reuse_address=True
def irc():
    srv=ThreadingTCPServer(('127.0.0.1',6667),IRC);srv.daemon_threads=True;srv.serve_forever()
class IRC(StreamRequestHandler):
    def handle(self):
        self.wfile.write(b':huddle 001 guest :Welcome\r\n:huddle 422 guest :MOTD File\r\n');self.wfile.flush()
        while True:
            x=self.rfile.readline()
            if not x:break
            if x.upper().startswith(b'PING'):
                self.wfile.write(b'PONG :huddle\r\n');self.wfile.flush()
            elif x.upper().startswith(b'QUIT'):break
def run():
    try: threading.Thread(target=lambda:RedisS(('127.0.0.1',6379),RedisH).serve_forever(),daemon=True).start()
    except: pass
    threading.Thread(target=irc,daemon=True).start()
    servers=[]
    for port in (8000,8001,8002):
        s=S(('127.0.0.1',port),H);threading.Thread(target=s.serve_forever,daemon=True).start();servers.append(s)
    print('huddle nodes ready',flush=True)
    while True: time.sleep(3600)

if __name__=='__main__':run()
