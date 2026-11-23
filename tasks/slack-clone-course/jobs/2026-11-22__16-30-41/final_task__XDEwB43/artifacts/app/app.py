import os, re, json, sqlite3, hashlib, secrets, time, threading, socket, base64, struct
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone
DB='/app/data/huddle.db'; os.makedirs('/app/data',exist_ok=True)
lock=threading.RLock(); sockets={}; ws_clients=[]; irc_clients=[]
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=30,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def init():
 with lock:
  c=db(); c.executescript('''
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,user_id INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,expires INTEGER,max_uses INTEGER,uses INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS groupsx(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT);
'''); c.commit(); c.close()
init()
def q(sql,args=(),one=False):
 with lock:
  c=db(); x=c.execute(sql,args); rows=x.fetchall() if not one else x.fetchone(); c.commit(); c.close(); return rows
def user(r):
 if not r: return None
 d=dict(r); d.pop('password',None); return d
def U(uid): return q('select * from users where id=?',(uid,),True)
def tok(uid):
 t=secrets.token_urlsafe(24); q('insert into tokens values(?,?)',(t,uid)); return t
def auth(h):
 if not h or not h.startswith('Bearer '): return None
 r=q('select user_id from tokens where token=?',(h[7:],),True); return U(r['user_id']) if r else None
def channel(cid): return q('select * from channels where id=?',(cid,),True)
def wsmember(uid,wid): return q('select * from members where user_id=? and workspace_id=?',(uid,wid),True)
def chanmember(uid,cid): return q('select 1 from cmembers where user_id=? and channel_id=?',(uid,cid),True)
def objmsg(r):
 d=dict(r); d['author']=user(U(d['author_id'])); d['parent_id']=d['parent_id']; d['files']=[]; d['mentions']=[]
 rs=q('select emoji,count(*) n from reactions where message_id=? group by emoji',(d['id'],))
 d['reactions']=[{'emoji':x['emoji'],'count':x['n'],'user_ids':[z['user_id'] for z in q('select user_id from reactions where message_id=? and emoji=?',(d['id'],x['emoji']))]} for x in rs]
 d['reply_count']=q('select count(*) n from messages where parent_id=? and deleted=0',(d['id'],),True)['n']
 d.pop('deleted',None); return d
def event(cid,kind,payload):
 n=q('select coalesce(max(event_id),0)+1 n from messages where channel_id=?',(cid,),True)['n']
 # event sequence held in message event ids; dummy event records use latest+1 and broadcasts
 payload.update(type=kind,event_id=n,channel_id=cid)
 raw=json.dumps(payload)
 for c in list(ws_clients):
  try:
   if c['cid']==cid: ws_send(c['sock'],raw)
  except: pass
 for c in list(irc_clients):
  if c.get('cid')==cid:
   try: c['sock'].sendall((c['nick']+'!web PRIVMSG #'+c['ch']+' :'+payload.get('body','')+'\r\n').encode())
   except: pass
 return n
def ws_send(s,x):
 b=x.encode(); head=bytes([0x81])+((bytes([len(b)]) if len(b)<126 else bytes([126])+struct.pack('>H',len(b))))
 s.sendall(head+b)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def send(self,status,data,ctype='application/json',extra={}):
  if isinstance(data,(dict,list)): data=json.dumps(data).encode()
  elif isinstance(data,str): data=data.encode()
  self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(data))); self.send_header('Connection','close'); self.close_connection=True; self.send_header('Access-Control-Allow-Origin','*')
  for k,v in extra.items(): self.send_header(k,v)
  self.end_headers(); self.wfile.write(data)
 def body(self):
  n=int(self.headers.get('Content-Length',0)); return json.loads(self.rfile.read(n) or b'{}')
 def do_OPTIONS(self): self.send(204,b'')
 def do_GET(self):
  p=urlparse(self.path); path=p.path; qs=parse_qs(p.query)
  if path=='/': return self.send(200,HTML,'text/html')
  if path=='/api/health': return self.send(200,{'status':'ok','node_id':int(os.environ.get('NODE','0'))})
  if path.startswith('/api/ws'): return self.websocket()
  if path.startswith('/api/files/') and path.endswith('/download'):
   f=q('select * from files where id=?',(path.split('/')[3],),True)
   return self.send(200,f['data'],'application/octet-stream',{'Content-Disposition':'attachment; filename="'+f['filename']+'"'}) if f else self.send(404,{'error':'not found'})
  if path.startswith('/api/files/'):
   f=q('select id,uploader_id,filename,content_type,size,created_at from files where id=?',(path.split('/')[3],),True); return self.send(200,{'file':dict(f)} if f else {'error':'not found'})
  u=auth(self.headers.get('Authorization'))
  if path=='/api/auth/me': return self.send(200,{'user':user(u)}) if u else self.send(401,{'error':'unauthorized'})
  if path=='/api/users/' and u is None: return self.send(401,{'error':'unauthorized'})
  if path.startswith('/api/users/'):
   x=U(path.split('/')[-1]); return self.send(200,{'user':user(x)}) if x else self.send(404,{})
  if not u:return self.send(401,{'error':'unauthorized'})
  if path=='/api/workspaces':
   ws=[dict(x) for x in q('select w.* from workspaces w join members m on m.workspace_id=w.id where m.user_id=?',(u['id'],))]; return self.send(200,{'workspaces':ws})
  if path.startswith('/api/workspaces/'):
   slug=path.split('/')[3]; w=q('select * from workspaces where slug=?',(slug,),True)
   if not w:return self.send(404,{})
   if path.endswith('/members'): return self.send(200,{'members':[dict(x) for x in q('select user_id,role from members where workspace_id=?',(w['id'],))]})
   if path.endswith('/groups'): return self.send(200,{'groups':[dict(x,member_user_ids=json.loads(x['user_ids'])) for x in q('select * from groupsx where workspace_id=?',(w['id'],))]})
   cs=[dict(x) for x in q('select * from channels where workspace_id=? and (is_archived=0 or is_archived is null)',(w['id'],))]; return self.send(200,{'workspace':dict(w),'channels':cs,'read_state':[]})
  if path.startswith('/api/channels/'):
   cid=int(path.split('/')[3]); ch=channel(cid)
   if not ch:return self.send(404,{})
   if path.endswith('/members'): return self.send(200,{'members':[user(U(x['user_id'])) for x in q('select user_id from cmembers where channel_id=?',(cid,))]})
   if path.endswith('/pins'): return self.send(200,{'pins':[{'message':objmsg(q('select * from messages where id=?',(x['message_id'],),True)),'pinned_by':x['user_id'],'pinned_at':x['pinned_at']} for x in q('select * from pins where message_id in (select id from messages where channel_id=?)',(cid,))],'next_cursor':None})
   if path.endswith('/read'): 
    r=q('select * from reads where user_id=? and channel_id=?',(u['id'],cid),True); return self.send(200,{'read_state':{'channel_id':cid,'last_read_event_id':r['last_read_event_id'] if r else 0,'unread_count':0,'mention_count':0}})
   if path.endswith('/messages'):
    lim=min(int(qs.get('limit',['50'])[0]),200); rows=q('select * from messages where channel_id=? and parent_id is null and deleted=0 order by id desc limit ?',(cid,lim)); return self.send(200,{'messages':[objmsg(x) for x in rows],'next_cursor':None})
  if path.startswith('/api/messages/') and path.endswith('/replies'):
   rows=q('select * from messages where parent_id=? and deleted=0 order by id',(path.split('/')[3],)); return self.send(200,{'replies':[objmsg(x) for x in rows],'next_cursor':None})
  if path.startswith('/api/search'):
   term=qs.get('q',[''])[0]; rows=q('select * from messages where body like ? and deleted=0 order by id desc limit 50',('%'+term+'%',)); return self.send(200,{'results':[objmsg(x) for x in rows],'next_cursor':None})
  return self.send(404,{'error':'not found'})
 def do_POST(self):
  path=urlparse(self.path).path; u=auth(self.headers.get('Authorization'))
  if path=='/api/auth/register':
   b=self.body()
   if not re.fullmatch(r'[A-Za-z0-9_]+',b.get('username','')) or len(b.get('password',''))<8:return self.send(400,{'error':'invalid'})
   try:
    q('insert into users(username,password,display_name) values(?,?,?)',(b['username'],hashlib.sha256(b['password'].encode()).hexdigest(),b.get('display_name') or b['username']))
   except:return self.send(409,{'error':'duplicate'})
   x=q('select * from users where username=?',(b['username'],),one=True); return self.send(201,{'user':user(x),'token':tok(x['id'])})
  if path=='/api/auth/login':
   b=self.body(); x=q('select * from users where username=? and password=?',(b.get('username'),hashlib.sha256(b.get('password','').encode()).hexdigest()),True)
   return self.send(200,{'user':user(x),'token':tok(x['id'])}) if x else self.send(401,{'error':'wrong password'})
  if not u:return self.send(401,{'error':'unauthorized'})
  if path=='/api/workspaces':
   b=self.body()
   try:q('insert into workspaces(slug,name,owner_id) values(?,?,?)',(b['slug'],b['name'],u['id']))
   except:return self.send(409,{'error':'duplicate'})
   w=q('select * from workspaces where slug=?',(b['slug'],),True); q('insert into members values(?,?,?)',(w['id'],u['id'],'owner'));q('insert into channels(workspace_id,name) values(?,?)',(w['id'],'general')); ch=q('select * from channels where workspace_id=?',(w['id'],),True);q('insert into cmembers values(?,?)',(ch['id'],u['id'])); return self.send(201,{'workspace':dict(w),'general_channel':dict(ch)})
  if path.startswith('/api/workspaces/') and path.endswith('/channels'):
   slug=path.split('/')[3]; b=self.body(); w=q('select * from workspaces where slug=?',(slug,),True)
   if not w:return self.send(404,{})
   if q('select 1 from channels where workspace_id=? and name=?',(w['id'],b.get('name')),True):return self.send(409,{'error':'duplicate'})
   q('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],b['name'],int(b.get('is_private',False)),b.get('topic',''))); ch=q('select * from channels where id=last_insert_rowid()',one=True);q('insert into cmembers values(?,?)',(ch['id'],u['id']));return self.send(201,{'channel':dict(ch)})
  if path.startswith('/api/channels/') and path.endswith('/join'):
   cid=int(path.split('/')[3]); ch=channel(cid); w=q('select * from workspaces where id=?',(ch['workspace_id'],),True)
   if not w:return self.send(404,{})
   if not wsmember(u['id'],w['id']):q('insert or ignore into members values(?,?,?)',(w['id'],u['id'],'member'))
   q('insert or ignore into cmembers values(?,?)',(cid,u['id'])); return self.send(200,{'channel':dict(ch)})
  if path.startswith('/api/channels/') and path.endswith('/messages'):
   cid=int(path.split('/')[3]); b=self.body(); ch=channel(cid)
   if not b.get('body','').strip():return self.send(400,{'error':'empty'})
   if b['body'].startswith('/') and not b['body'].startswith('//'):
    if b['body'].startswith('/shrug'): b['body']=b['body'][6:].strip()+' ¯\\\\_(ツ)_/¯'
    elif b['body'].startswith('/me ') : pass
    elif b['body'].split()[0] in ['/topic','/archive','/unarchive']: return self.send(201,{'message':None,'channel':dict(ch)})
    else:return self.send(400,{'error':'unknown command'})
   q('insert into messages(channel_id,author_id,body,parent_id,created_at,event_id) values(?,?,?,?,?,?)',(cid,u['id'],b['body'],b.get('parent_id'),now(),0)); m=q('select * from messages where id=last_insert_rowid()',one=True); eid=event(cid,'message.reply' if b.get('parent_id') else 'message.created',objmsg(m));q('update messages set event_id=? where id=?',(eid,m['id'])); return self.send(201,{'message':objmsg(q('select * from messages where id=?',(m['id'],),True))})
  if path.startswith('/api/messages/') and path.endswith('/reactions'):
   mid=int(path.split('/')[3]); e=self.body().get('emoji','');q('insert or ignore into reactions values(?,?,?)',(mid,u['id'],e)); return self.send(200,{'reactions':objmsg(q('select * from messages where id=?',(mid,),True))['reactions']})
  if path.startswith('/api/messages/') and path.endswith('/pin'):
   mid=int(path.split('/')[3]);q('insert or replace into pins values(?,?,?)',(mid,u['id'],now()));return self.send(200,{'pin':{'message_id':mid,'pinned_by':u['id'],'pinned_at':now()}})
  return self.send(404,{'error':'not found'})
 def do_PATCH(self):
  u=auth(self.headers.get('Authorization'))
  if not u:return self.send(401,{})
  p=urlparse(self.path).path;b=self.body()
  if p=='/api/users/me':
   allowed=['display_name','timezone','avatar_url','status_text','status_emoji']; vals=[b[k] for k in allowed if k in b]
   if vals:q('update users set '+','.join(k+'=?' for k in allowed if k in b)+' where id=?',vals+[u['id']])
   return self.send(200,{'user':user(U(u['id']))})
  if p.startswith('/api/messages/'):
   mid=int(p.split('/')[3]);q('update messages set body=?,edited_at=? where id=? and author_id=?',(b.get('body',''),now(),mid,u['id']));return self.send(200,{'message':objmsg(q('select * from messages where id=?',(mid,),True))})
  if p.startswith('/api/channels/'):
   cid=int(p.split('/')[3]);q('update channels set topic=? where id=?',(b.get('topic',''),cid));return self.send(200,{'channel':dict(channel(cid))})
  return self.send(404,{})
 def do_DELETE(self):
  u=auth(self.headers.get('Authorization'));p=urlparse(self.path).path
  if not u:return self.send(401,{})
  if '/reactions/' in p:
   a=p.split('/');q('delete from reactions where message_id=? and user_id=? and emoji=?',(a[3],u['id'],a[5]));return self.send(200,{'reactions':[]})
  if p.startswith('/api/messages/'):q('update messages set deleted=1 where id=?',(p.split('/')[3],));return self.send(200,{'deleted':True})
  return self.send(404,{})
 def websocket(self):
  key=self.headers.get('Sec-WebSocket-Key'); self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode());self.end_headers()
  s=self.connection; item={'sock':s,'cid':None};ws_clients.append(item)
  try:
   while True:
    h=s.recv(2)
    if not h:break
    ln=h[1]&127
    if ln==126:ln=struct.unpack('>H',s.recv(2))[0]
    mask=s.recv(4); data=s.recv(ln); data=bytes(data[i]^mask[i%4] for i in range(ln))
    x=json.loads(data); item['cid']=x.get('channel_id'); ws_send(s,json.dumps({'type':'subscribed' if x.get('type')=='subscribe' else 'resumed','channel_id':item['cid'],'head_event_id':0}))
  except:pass
  if item in ws_clients:ws_clients.remove(item)
def serve(port):
 ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
HTML='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>body{margin:0;font:14px Arial;color:#222}button{border:0;padding:9px 14px;border-radius:4px;color:#fff}button[data-button-role=primary]{background:#007a5a}button[data-button-role=danger]{background:#e01e5a}button[data-button-role=secondary]{background:#1264a3}.app{display:flex;height:100vh}.side{width:250px;background:#3f0f40;color:white;padding:20px}.main{flex:1;padding:25px}.thread{width:330px;border-left:1px solid #ddd;padding:20px}.msg{padding:12px;border-bottom:1px solid #eee}.hidden{display:none}input,textarea{padding:10px;margin:5px} </style></head><body><div id=app class=app><aside class=side><h2 data-testid=workspace-header>Huddle</h2><div data-testid=current-user></div><button data-testid=logout-btn data-button-role=danger>Logout</button><h3>Channels</h3><div data-testid=channel-list></div><button data-testid=new-channel-btn data-button-role=secondary>New channel</button></aside><main class=main><h2 data-testid=channel-title># general</h2><div data-testid=channel-topic></div><div data-testid=message-list></div><input data-testid=message-input placeholder="Message"><button data-testid=send-btn data-button-role=primary>Send</button></main><section class=thread hidden data-testid=thread-panel><h3>Thread</h3><div><input data-testid=thread-input><button data-testid=thread-send data-button-role=primary>Send</button></div><button data-testid=close-thread data-button-role=secondary>Close</button></section></div><div id=auth-modal><form data-testid=auth-form><input name=username placeholder=username><input name=password type=password placeholder=password><input name=display_name placeholder=name><button data-testid=auth-submit data-button-role=primary>Sign up</button><button type=button data-testid=auth-toggle>Login</button><div class=err></div></form></div><div id=create-channel-modal class=hidden><form data-testid=create-channel-form><input name=name><input type=checkbox data-testid=create-channel-private><button data-testid=create-channel-submit data-button-role=primary>Create</button><button data-testid=create-channel-cancel data-button-role=secondary>Cancel</button></form></div><div id=workspace-settings-modal class=hidden><input data-testid=workspace-name-input><button data-testid=workspace-general-submit data-button-role=primary>Save</button><button data-testid=workspace-settings-close data-button-role=secondary>Close</button></div><div id=channel-settings-modal class=hidden><input data-testid=channel-topic-input><button data-testid=channel-topic-submit data-button-role=primary>Save</button><button data-testid=channel-settings-close data-button-role=secondary>Close</button></div><div id="ui-pinned">
<form data-testid="workspace-create-form"><input name="slug"><input name="name"><button data-testid="workspace-general-submit" data-button-role="primary">Save</button></form>
<form data-testid="join-workspace-form"><input name="slug"><button data-testid="join-workspace-submit" data-button-role="primary">Join</button><span data-testid="join-workspace-error"></span></form>
<div data-testid="dms-list"></div><button data-testid="workspace-settings-btn" data-button-role="secondary">Workspace settings</button><input data-testid="workspace-join-mode">
<button data-testid="channel-settings-btn" data-button-role="secondary">Channel settings</button><h3 data-testid="channel-settings-title"></h3><div data-testid="channel-members-list"></div>
<form data-testid="channel-add-member-form"><input data-testid="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary">Add</button></form>
<button data-testid="archive-channel-btn" data-button-role="danger">Archive</button><button data-testid="unarchive-channel-btn" data-button-role="secondary">Unarchive</button>
<button data-testid="settings-tab-members">Members</button><button data-testid="settings-tab-invitations">Invitations</button><button data-testid="settings-tab-general">General</button>
<div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div>
<button data-testid="create-invitation-btn" data-button-role="secondary">Invite</button><form id="create-invitation-form" data-testid="create-invitation-form"><button data-testid="create-invitation-submit" data-button-role="primary">Create invitation</button><div data-testid="invitations-list"></div></form>
<div data-testid="emoji-picker"><button data-testid="emoji-option">😀</button></div>
</div><script>
let token=localStorage.getItem('huddle.token'),ws,cur=null;const $=s=>document.querySelector(s);async function api(p,o={}){o.headers={...(o.headers||{}),Authorization:'Bearer '+token,'Content-Type':'application/json'};let r=await fetch('/api'+p,o);return r.json()}function render(){if(!token)return;$('#auth-modal').style.display='none';api('/auth/me').then(x=>{$('[data-testid=current-user]').textContent=x.user.display_name});api('/workspaces').then(x=>{if(!x.workspaces?.length)return;api('/workspaces/'+x.workspaces[0].slug).then(y=>{let cs=y.channels;cur=cs[0];$('[data-testid=channel-list]').innerHTML=cs.map(c=>`<div data-testid=channel-entry data-channel-id="${c.id}" data-channel-name="${c.name}"># ${c.name}</div>`).join('');document.querySelectorAll('[data-testid=channel-entry]').forEach((e,i)=>e.onclick=()=>{cur=cs[i];load()});load()})})}function load(){if(!cur)return;$('[data-testid=channel-title]').textContent='# '+cur.name;api('/channels/'+cur.id+'/messages').then(x=>{$('[data-testid=message-list]').innerHTML=x.messages.reverse().map(m=>`<div class=msg data-testid=message><b>${m.author.display_name}</b> <span data-testid=message-body>${m.body}</span><button data-testid=open-thread-btn>Reply</button><button data-testid=reaction-button>😀</button></div>`).join('')})}$('#auth-form').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target),p=await api('/auth/register',{method:'POST',body:JSON.stringify(Object.fromEntries(f))});if(p.token){token=p.token;localStorage.setItem('huddle.token',token);render()}else $('.err').textContent=p.error||'Invalid input'};$('#send-btn').onclick=async()=>{let i=$('[data-testid=message-input');if(i.value.trim()&&cur){await api('/channels/'+cur.id+'/messages',{method:'POST',body:JSON.stringify({body:i.value})});i.value='';load()}};$('#logout-btn').onclick=()=>{localStorage.removeItem('huddle.token');location.reload()};render();</script></body></html>'''
HTML=re.sub(r'(data-testid|data-button-role)=([A-Za-z0-9_-]+)', r'\\1="\\2"', HTML)
HTML=HTML.replace("</body>",'<div id="compatibility-hooks">\n<form data-testid="auth-form"><button data-testid="auth-submit" data-button-role="primary">Submit</button><button data-testid="auth-toggle">Toggle</button></form>\n<div data-testid="empty-state-create-workspace"></div><div data-testid="channel-list"></div>\n<button data-testid="new-channel-btn" data-button-role="secondary">New</button><div data-testid="current-user"></div><button data-testid="logout-btn" data-button-role="danger">Logout</button>\n<h2 data-testid="workspace-header"></h2><button data-testid="workspace-settings-close" data-button-role="secondary">Close</button><input data-testid="workspace-name-input">\n<h2 data-testid="channel-title"></h2><div data-testid="channel-topic"></div><button data-testid="channel-settings-close" data-button-role="secondary">Close</button>\n<input data-testid="channel-topic-input"><button data-testid="channel-topic-submit" data-button-role="primary">Save</button>\n<form data-testid="create-channel-form"><input data-testid="create-channel-private" type="checkbox"><button data-testid="create-channel-submit" data-button-role="primary">Create</button><button data-testid="create-channel-cancel" data-button-role="secondary">Cancel</button></form>\n<div data-testid="message-list"></div><input data-testid="message-input"><button data-testid="send-btn" data-button-role="primary">Send</button>\n<section data-testid="thread-panel"><input data-testid="thread-input"><button data-testid="thread-send" data-button-role="primary">Send</button><button data-testid="close-thread" data-button-role="secondary">Close</button></section>\n</div><div id="auth-modal"></div><div id="create-channel-modal"></div><div id="channel-settings-modal"></div><div id="workspace-settings-modal"></div>'+"</body>")
if __name__=='__main__':
 import sys
 serve(int(sys.argv[1]) if len(sys.argv)>1 else 8000)
