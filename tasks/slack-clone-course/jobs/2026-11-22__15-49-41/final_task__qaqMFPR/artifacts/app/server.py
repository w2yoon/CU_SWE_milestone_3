import os, re, json, sqlite3, hashlib, secrets, mimetypes, html
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.parse import unquote
DB='/app/huddle.db'
PORT=int(os.environ.get('PORT','8000'))
NODE=PORT-8000
def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    c=sqlite3.connect(DB, timeout=20, check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=10000')
    return c
def init():
    c=db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,user_id INTEGER,pinned_at TEXT);
    CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
    CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT,UNIQUE(workspace_id,handle));
    CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,expires_at TEXT);
    CREATE TABLE IF NOT EXISTS seq(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
    ''')
    c.commit(); c.close()
def obj_user(r):
    return dict(r) if r else None
def user(c,uid):
    return c.execute('SELECT id,username,display_name,timezone,avatar_url,status_text,status_emoji FROM users WHERE id=?',(uid,)).fetchone()
def chan(c,cid):
    return c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
def message(c,r):
    d=dict(r); d['author']=obj_user(user(c,d['author_id'])); d['parent_id']=d['parent_id']; d['files']=[]; d['mentions']=[]
    d['reactions']=[]
    for x in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(d['id'],)):
        d['reactions'].append({'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]})
    return d
def workspace(c,r):
    return dict(r)
def auth(c,h):
    if not h.startswith('Bearer '): return None
    r=c.execute('SELECT user_id FROM tokens WHERE token=?',(h[7:],)).fetchone()
    return r['user_id'] if r else None
class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def send(self,status,data=None,ctype='application/json',headers=None):
        self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Access-Control-Allow-Origin','*'); self.send_header('Access-Control-Allow-Headers','Authorization,Content-Type'); self.send_header('Access-Control-Allow-Methods','GET,POST,PATCH,DELETE,OPTIONS')
        if headers:
            for k,v in headers.items(): self.send_header(k,v)
        if data is None: self.end_headers(); return
        b=data if isinstance(data,bytes) else json.dumps(data).encode()
        self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def body(self):
        n=int(self.headers.get('Content-Length','0')); 
        try:return json.loads(self.rfile.read(n) or b'{}')
        except:return {}
    def route(self,method):
        p=urlparse(self.path); path=p.path; q=parse_qs(p.query); c=db()
        if method=='OPTIONS': self.send(204); return
        if path=='/api/health': self.send(200,{'status':'ok','node_id':NODE}); return
        if path=='/' and method=='GET': self.send(200,HTML,'text/html'); return
        b=self.body() if method in ('POST','PATCH') else {}
        uid=auth(c,self.headers.get('Authorization',''))
        if path=='/api/auth/register' and method=='POST':
            un=str(b.get('username','')); pw=str(b.get('password',''))
            if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: self.send(400,{'error':'invalid registration'}); return
            try:
                cur=c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),b.get('display_name') or un)); uid=cur.lastrowid
                tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid)); c.commit()
                self.send(201,{'user':obj_user(user(c,uid)),'token':tok})
            except sqlite3.IntegrityError:self.send(409,{'error':'duplicate username'})
            return
        if path=='/api/auth/login' and method=='POST':
            r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),hashlib.sha256(str(b.get('password','')).encode()).hexdigest())).fetchone()
            if not r:self.send(401,{'error':'invalid credentials'});return
            tok=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(tok,r['id']));c.commit();self.send(200,{'user':obj_user(user(c,r['id'])),'token':tok});return
        if path=='/api/auth/me':
            if not uid:self.send(401,{'error':'unauthorized'});return
            self.send(200,{'user':obj_user(user(c,uid))});return
        if not uid:
            self.send(401,{'error':'unauthorized'});return
        if path=='/api/users/me' and method=='PATCH':
            allowed=['display_name','timezone','avatar_url','status_text','status_emoji']; vals={k:b[k] for k in allowed if k in b}
            if vals:
                c.execute('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',(*vals.values(),uid));c.commit()
            self.send(200,{'user':obj_user(user(c,uid))});return
        if path=='/api/workspaces' and method=='GET':
            rs=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(uid,)).fetchall();self.send(200,{'workspaces':[workspace(c,x) for x in rs]});return
        if path=='/api/workspaces' and method=='POST':
            slug=str(b.get('slug','')); name=str(b.get('name',''))
            if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not name:self.send(400,{'error':'invalid workspace'});return
            try:
                x=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,name,uid));wid=x.lastrowid
                c.execute('INSERT INTO members VALUES(?,?,?)',(wid,uid,'owner')); z=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=z.lastrowid;c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,uid));c.commit()
                self.send(201,{'workspace':workspace(c,c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone()),'general_channel':dict(chan(c,cid))})
            except sqlite3.IntegrityError:self.send(409,{'error':'duplicate slug'})
            return
        m=re.fullmatch(r'/api/workspaces/([^/]+)',path)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(unquote(m.group(1)),)).fetchone()
            if not w:self.send(404,{'error':'not found'});return
            if method=='GET':
                cs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0',(w['id'],)).fetchall();self.send(200,{'workspace':workspace(c,w),'channels':[dict(x) for x in cs],'read_state':{}});return
            if method=='PATCH':
                vals={k:b[k] for k in ('name','join_mode') if k in b}
                if vals:c.execute('UPDATE workspaces SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',(*vals.values(),w['id']));c.commit()
                self.send(200,{'workspace':workspace(c,c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())});return
        m=re.fullmatch(r'/api/workspaces/([^/]+)/channels',path)
        if m and method=='POST':
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone();name=str(b.get('name',''))
            if not w or not re.fullmatch(r'[a-z0-9-]{1,32}',name):self.send(400,{'error':'invalid channel'});return
            role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone()
            if not role:self.send(403,{'error':'not member'});return
            if b.get('is_private') and role['role'] not in ('owner','admin'):self.send(403,{'error':'forbidden'});return
            try:
                x=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(b.get('is_private'))),b.get('topic','')));cid=x.lastrowid;c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,uid));c.commit();self.send(201,{'channel':dict(chan(c,cid))})
            except sqlite3.IntegrityError:self.send(409,{'error':'duplicate channel'})
            return
        m=re.fullmatch(r'/api/channels/(\d+)(?:/(.*))?',path)
        if m:
            cid=int(m.group(1)); ch=chan(c,cid)
            if not ch:self.send(404,{'error':'not found'});return
            if m.group(2)=='join' and method=='POST':
                c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,uid));c.execute('INSERT OR IGNORE INTO members(workspace_id,user_id,role) VALUES(?,?,?)',(ch['workspace_id'],uid,'member'));c.commit();self.send(200,{'channel':dict(ch)});return
            if m.group(2)=='members' and method=='GET':
                rs=c.execute('SELECT u.id,u.username,u.display_name,u.timezone,u.avatar_url,u.status_text,u.status_emoji FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(cid,)).fetchall();self.send(200,{'members':[obj_user(x) for x in rs]});return
            if m.group(2) in ('messages',None) and method=='GET':
                rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC LIMIT 200',(cid,)).fetchall();self.send(200,{'messages':[message(c,x) for x in rs],'next_cursor':None});return
            if m.group(2)=='messages' and method=='POST':
                body=str(b.get('body',''))
                if not body.strip():self.send(400,{'error':'message cannot be empty'});return
                n=c.execute('SELECT n FROM seq WHERE channel_id=?',(cid,)).fetchone();eid=(n['n']+1 if n else 1);c.execute('INSERT OR REPLACE INTO seq VALUES(?,?)',(cid,eid));x=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,event_id) VALUES(?,?,?,?,?,?)',(cid,uid,body,b.get('parent_id'),now(),eid));c.commit();self.send(201,{'message':message(c,c.execute('SELECT * FROM messages WHERE id=?',(x.lastrowid,)).fetchone())});return
        if path=='/api/dms' and method=='POST':
            other=int(b.get('user_id',0))
            if not user(c,other): self.send(404,{'error':'user not found'});return
            a,bid=sorted((uid,other)); dmname='dm-'+str(a)+'-'+str(bid)
            w=c.execute('SELECT id FROM workspaces WHERE slug=?',(dmname,)).fetchone()
            if w:
                z=c.execute('SELECT id FROM channels WHERE workspace_id=?',(w['id'],)).fetchone()
                self.send(200,{'channel_id':z['id']});return
            z=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(dmname,dmname,uid));wid=z.lastrowid
            c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(wid,uid,'owner'));c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(wid,other,'member'))
            z=c.execute('INSERT INTO channels(workspace_id,name,is_dm) VALUES(?,?,1)',(wid,'direct'));cid=z.lastrowid
            c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,uid));c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,other));c.commit()
            self.send(200,{'channel_id':cid});return
        m=re.fullmatch(r'/api/messages/(\d+)/pin',path)
        if m and method=='POST':
            x=c.execute('SELECT id FROM messages WHERE id=? AND deleted=0',(int(m.group(1)),)).fetchone()
            if not x:self.send(404,{'error':'not found'});return
            c.execute('INSERT OR IGNORE INTO pins(message_id,user_id,pinned_at) VALUES(?,?,?)',(x['id'],uid,now()));c.commit()
            z=c.execute('SELECT * FROM pins WHERE message_id=?',(x['id'],)).fetchone()
            self.send(200,{'pin':{'message_id':z['message_id'],'pinned_by':z['user_id'],'pinned_at':z['pinned_at']}});return
        if m and method=='DELETE':
            c.execute('DELETE FROM pins WHERE message_id=?',(int(m.group(1)),));c.commit();self.send(200,{'unpinned':True});return
        m=re.fullmatch(r'/api/channels/(\d+)/pins',path)
        if m and method=='GET':
            rs=c.execute('SELECT p.message_id,p.user_id,p.pinned_at FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC',(int(m.group(1)),)).fetchall()
            self.send(200,{'pins':[{'message':message(c,c.execute('SELECT * FROM messages WHERE id=?',(z['message_id'],)).fetchone()),'pinned_by':z['user_id'],'pinned_at':z['pinned_at']} for z in rs],'next_cursor':None});return
        m=re.fullmatch(r'/api/channels/(\d+)/read',path)
        if m:
            key='/tmp/read-'+str(uid)+'-'+m.group(1)
            if method=='GET':
                try:last=int(open(key).read())
                except:last=0
                self.send(200,{'read_state':{'channel_id':int(m.group(1)),'last_read_event_id':last,'unread_count':0,'mention_count':0}});return
            if method=='POST':
                try: old=int(open(key).read())
                except: old=0
                last=max(old,int(b.get('last_read_event_id',0)))
                open(key,'w').write(str(last))
                self.send(200,{'read_state':{'channel_id':int(m.group(1)),'last_read_event_id':last,'unread_count':0,'mention_count':0}});return
        m=re.fullmatch(r'/api/users/(\d+)',path)
        if m and method=='GET':
            x=user(c,int(m.group(1)))
            self.send(200,{'user':obj_user(x)}) if x else self.send(404,{'error':'not found'})
            return
        m=re.fullmatch(r'/api/workspaces/([^/]+)/members',path)
        if m and method=='GET':
            w=c.execute('SELECT id FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            if not w:self.send(404,{'error':'not found'});return
            rs=c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],)).fetchall()
            self.send(200,{'members':[dict(z) for z in rs]});return
        m=re.fullmatch(r'/api/channels/(\d+)',path)
        if m and method=='PATCH':
            ch=chan(c,int(m.group(1)))
            if not ch:self.send(404,{'error':'not found'});return
            if len(str(b.get('topic','')))>250:self.send(400,{'error':'topic too long'});return
            c.execute('UPDATE channels SET topic=? WHERE id=?',(str(b.get('topic',ch['topic'])),ch['id']));c.commit()
            self.send(200,{'channel':dict(chan(c,ch['id']))});return
        m=re.fullmatch(r'/api/messages/(\d+)/replies',path)
        if m and method=='GET':
            rs=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(int(m.group(1)),)).fetchall()
            self.send(200,{'replies':[message(c,z) for z in rs],'next_cursor':None});return
        m=re.fullmatch(r'/api/messages/(\d+)/reactions',path)
        if m and method=='POST':
            emoji=str(b.get('emoji',''))
            if not emoji:self.send(400,{'error':'emoji required'});return
            c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(int(m.group(1)),uid,emoji));c.commit()
            rs=c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(int(m.group(1)),)).fetchall()
            self.send(200,{'reactions':[{'emoji':z['emoji'],'count':z['n'],'user_ids':[int(q) for q in z['ids'].split(',')]} for z in rs]});return
        m=re.fullmatch(r'/api/messages/(\d+)/reactions/(.+)',path)
        if m and method=='DELETE':
            c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(int(m.group(1)),uid,unquote(m.group(2))));c.commit()
            self.send(200,{'reactions':[]});return
        m=re.fullmatch(r'/api/messages/(\d+)',path)
        if m:
            x=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(int(m.group(1)),)).fetchone()
            if not x:self.send(404,{'error':'not found'});return
            if method=='PATCH':
                if x['author_id']!=uid:self.send(403,{'error':'forbidden'});return
                c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(str(b.get('body','')),now(),x['id']));c.commit();self.send(200,{'message':message(c,c.execute('SELECT * FROM messages WHERE id=?',(x['id'],)).fetchone())});return
            if method=='DELETE':
                c.execute('UPDATE messages SET deleted=1 WHERE id=?',(x['id'],));c.commit();self.send(200,{'deleted':True});return
        if path=='/api/search' and method=='GET':
            term=parse_qs(urlparse(self.path).query).get('q',[''])[0];rs=c.execute('SELECT * FROM messages WHERE deleted=0 AND body LIKE ? ORDER BY id DESC LIMIT 200',('%'+term+'%',)).fetchall();self.send(200,{'results':[message(c,x) for x in rs],'next_cursor':None});return
        self.send(404,{'error':'not found'})
    def do_GET(self):
        try:self.route('GET')
        except Exception as e:self.send(500,{'error':str(e)})
    def do_POST(self):
        try:self.route('POST')
        except Exception as e:self.send(500,{'error':str(e)})
    def do_PATCH(self):
        try:self.route('PATCH')
        except Exception as e:self.send(500,{'error':str(e)})
    def do_DELETE(self):
        try:self.route('DELETE')
        except Exception as e:self.send(500,{'error':str(e)})
    def do_OPTIONS(self): self.route('OPTIONS')
HTML='''<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><style>
*{box-sizing:border-box}body{margin:0;font-family:Arial;color:#222}button{border:0;border-radius:4px;padding:10px 16px;color:#fff;cursor:pointer}.primary{background:#007a5a}.danger{background:#e01e5a}.secondary{background:#1264a3}.app{display:flex;height:100vh}.side{width:260px;background:#3f0e40;color:white;padding:20px}.main{flex:1;padding:28px;max-width:900px}.card{background:white;border:1px solid #ddd;padding:16px;margin:10px 0;border-radius:6px}input,textarea,select{padding:10px;border:1px solid #bbb;border-radius:4px;margin:5px 0;width:100%}.hidden{display:none}.msg{padding:14px;border-bottom:1px solid #eee}.error{color:#e01e5a;margin:8px 0}.channel{padding:9px;cursor:pointer}.channel:hover,.channel.active{background:#6b2b68;border-radius:4px}</style></head><body>
<div id="root"><div id="auth-modal" class="card" style="max-width:420px;margin:60px auto"><h1>Huddle</h1><form id="auth-form" data-testid="auth-form"><input name="username" placeholder="Username" required><input name="password" type="password" placeholder="Password (8+ chars)" required><input name="display_name" placeholder="Display name"><div id="auth-error" class="error"></div><button id="auth-submit" data-testid="auth-submit" data-button-role="primary" class="primary">Log in</button><button type="button" id="auth-toggle" data-testid="auth-toggle" class="secondary">Sign up</button></form></div></div>
<script>
const $=s=>document.querySelector(s), api=async(u,o={})=>{o.headers={...(o.headers||{}),'Content-Type':'application/json',...(localStorage.getItem('huddle.token')?{Authorization:'Bearer '+localStorage.getItem('huddle.token')}:{})};let r=await fetch(u,o),j=await r.json().catch(()=>({}));if(!r.ok)throw Error(j.error||'Request failed');return j};
let signup=false;$('#auth-toggle').onclick=()=>{signup=!signup;$('#auth-submit').textContent=signup?'Create account':'Log in';$('#auth-toggle').textContent=signup?'Log in':'Sign up'};
$('#auth-form').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target),b=Object.fromEntries(f);try{let j=await api('/api/auth/'+(signup?'register':'login'),{method:'POST',body:JSON.stringify(b)});localStorage.setItem('huddle.token',j.token);load()}catch(x){$('#auth-error').textContent=x.message}};
async function load(){let ws=(await api('/api/workspaces')).workspaces;if(!ws.length){render([]);return}let w=await api('/api/workspaces/'+ws[0].slug);render(w.channels,w.workspace)}
function render(chs,w){$('#root').innerHTML='<div class="app"><aside class="side"><h2 data-testid="workspace-header">'+(w?.name||'Huddle')+'</h2><div id="current-user" data-testid="current-user"></div><button id="logout-btn" data-testid="logout-btn" data-button-role="danger" class="danger">Logout</button><h3>Channels</h3><div id="channel-list" data-testid="channel-list">'+chs.map(c=>`<div class="channel channel-entry" data-testid="channel-entry" data-channel-id="${c.id}" data-channel-name="${c.name}"># ${c.name}</div>`).join('')+'</div><button id="new-channel-btn" data-testid="new-channel-btn" data-button-role="secondary" class="secondary">New channel</button></aside><main class="main"><h1 id="channel-title" data-testid="channel-title">Select a channel</h1><div id="channel-topic" data-testid="channel-topic"></div><div id="message-list" data-testid="message-list"></div><form id="send" class="hidden"><input id="message-input" data-testid="message-input" placeholder="Message"><button id="send-btn" data-testid="send-btn" data-button-role="primary" class="primary">Send</button></form></main></div>';
$('#logout-btn').onclick=()=>{localStorage.removeItem('huddle.token');location.reload()};document.querySelectorAll('.channel-entry').forEach(x=>x.onclick=()=>open(x.dataset.channelId,x.dataset.channelName));}
async function open(id,name){$('#channel-title').textContent='# '+name;$('#send').classList.remove('hidden');let j=await api('/api/channels/'+id+'/messages');$('#message-list').innerHTML=j.messages.reverse().map(m=>`<div class="msg" data-testid="message"><b>${m.author.display_name||m.author.username}</b> <small>${m.created_at}</small><div data-testid="message-body">${m.body}</div></div>`).join('')||'<div class="card">No messages yet. Start the conversation!</div>';$('#send').onsubmit=async e=>{e.preventDefault();let i=$('#message-input');try{await api('/api/channels/'+id+'/messages',{method:'POST',body:JSON.stringify({body:i.value})});i.value='';open(id,name)}catch(x){alert(x.message)}}}
if(localStorage.getItem('huddle.token'))load();
</script></body></html>'''
if __name__=='__main__':
    init(); ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
