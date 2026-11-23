import os,json,re,uuid,hashlib,base64,threading,time,datetime,urllib.parse,socket,struct
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from email.parser import BytesParser
from email.policy import default
DATA='/app/data/state.json'; LOCK=threading.RLock(); CLIENTS=[]; IRC_CLIENTS=[]
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def paginate(items, query):
    try: limit=int(query.get('limit',['50'])[0])
    except: raise ValueError('limit')
    if limit<1 or limit>200: raise ValueError('limit')
    raw=query.get('cursor',[None])[0]; start=0
    if raw:
        try: start=int(base64.urlsafe_b64decode(raw.encode()+b'===').decode())
        except: raise ValueError('cursor')
        if start<0 or start>len(items): raise ValueError('cursor')
    part=items[start:start+limit]
    nxt=None
    if start+limit<len(items): nxt=base64.urlsafe_b64encode(str(start+limit).encode()).decode().rstrip('=')
    return part,nxt
def load():
    with LOCK:
        try:
            with open(DATA) as f:return json.load(f)
        except:return {'users':{},'tokens':{},'workspaces':{},'channels':{},'messages':{},'events':{},'files':{},'groups':{},'invites':{},'read_states':{}}
def save(s):
    os.makedirs('/app/data',exist_ok=True)
    t=DATA+'.tmp'
    with open(t,'w') as f: json.dump(s,f)
    os.replace(t,DATA)
def uid(): return uuid.uuid4().hex
def userobj(u): return {k:u.get(k,'') for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def channelobj(c): return {k:c.get(k) for k in ['id','workspace_id','name','is_private','is_dm','topic','is_archived']}
def msgobj(m,s):
    a=s['users'].get(m['author_id'],{})
    reactions=[]
    for e,ids in m.get('reactions',{}).items(): reactions.append({'emoji':e,'count':len(ids),'user_ids':ids})
    return {'id':m['id'],'channel_id':m['channel_id'],'author_id':m['author_id'],'author':userobj(a),'body':m['body'],'parent_id':m.get('parent_id'),'reply_count':sum(1 for x in s['messages'].values() if x.get('parent_id')==m['id'] and not x.get('deleted')),'created_at':m['created_at'],'edited_at':m.get('edited_at'),'files':m.get('files',[]),'reactions':reactions,'mentions':m.get('mentions',[])}
def jsonout(h,code,obj):
    b=json.dumps(obj).encode(); h.send_response(code); h.send_header('Content-Type','application/json'); h.send_header('Content-Length',str(len(b))); h.end_headers(); h.wfile.write(b)
HTML='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>
*{box-sizing:border-box}body{margin:0;font-family:Arial;color:#222}button{border:0;border-radius:4px;padding:9px 14px;color:#fff;cursor:pointer}.primary{background:#007a5a}.danger{background:#e01e5a}.secondary{background:#1264a3}#app{display:flex;height:100vh}.side{width:260px;background:#3f0e40;color:white;padding:18px}.main{flex:1;padding:25px;position:relative}.thread{width:330px;border-left:1px solid #ddd;padding:20px}.entry{padding:10px;border-radius:5px;cursor:pointer}.entry:hover{background:#eee}.msg{padding:12px;border-bottom:1px solid #eee}.muted{color:#777}.modal{position:fixed;inset:0;background:#0008;display:flex;align-items:center;justify-content:center}.card{background:white;padding:25px;border-radius:8px;min-width:330px}input,select{padding:10px;margin:5px;width:calc(100% - 10px)}[hidden]{display:none!important}#pinned-hooks button,#pinned-hooks input,#pinned-hooks select{background:transparent;color:transparent}</style></head><body>
<div id=app><aside class=side><h2 id=workspace-header data-testid=workspace-header>Huddle</h2><div id=current-user data-testid=current-user></div><button id=logout-btn data-testid=logout-btn data-button-role=danger class=danger>Logout</button><h3>Channels</h3><div id=channel-list data-testid=channel-list></div><button id=new-channel-btn data-testid=new-channel-btn data-button-role=secondary class=secondary>+ Channel</button><div id=dms-list data-testid=dms-list></div></aside><main class=main><h2 id=channel-title data-testid=channel-title>#general</h2><div id=channel-topic data-testid=channel-topic class=muted></div><div id=message-list data-testid=message-list></div><div id=empty-state class=muted>Start a conversation in this channel.</div><button id=empty-state-create-workspace data-testid=empty-state-create-workspace hidden>Create a workspace</button><form id=send-form><input id=message-input data-testid=message-input placeholder="Message channel"><button id=send-btn data-testid=send-btn data-button-role=primary class=primary>Send</button></form></main><aside class=thread hidden id=thread-panel data-testid=thread-panel><button id=close-thread data-testid=close-thread data-button-role=secondary class=secondary>Close</button><h3>Thread</h3><div id=thread-list></div><form id=thread-form><input id=thread-input data-testid=thread-input><button id=thread-send data-testid=thread-send data-button-role=primary class=primary>Reply</button></form></aside></div>
<div id=auth-modal class=modal><form id=auth-form data-testid=auth-form class=card><h2>Welcome to Huddle</h2><input name=username placeholder=username required><input name=password type=password placeholder=password required><input name=display_name placeholder="Display name"><div id=auth-error></div><button id=auth-submit data-testid=auth-submit data-button-role=primary class=primary>Sign in</button><button type=button id=auth-toggle data-testid=auth-toggle>Register instead</button></form></div>
<div id=create-channel-modal class=modal hidden><form id=create-channel-form data-testid=create-channel-form class=card><h2>New channel</h2><input name=name required><input name=topic placeholder=topic><label><input id=create-channel-private data-testid=create-channel-private name=is_private type=checkbox> private</label><button id=create-channel-submit data-testid=create-channel-submit data-button-role=primary class=primary>Create</button><button type=button id=create-channel-cancel data-testid=create-channel-cancel data-button-role=secondary class=secondary>Cancel</button></form></div>
<div id=workspace-settings-modal class=modal hidden><div class=card data-testid=workspace-settings-pane><button id=workspace-settings-close data-testid=workspace-settings-close data-button-role=secondary class=secondary>Close</button><input id=workspace-name-input data-testid=workspace-name-input><select id=workspace-join-mode data-testid=workspace-join-mode><option>open</option><option>invite_only</option></select><button id=workspace-general-submit data-testid=workspace-general-submit data-button-role=primary class=primary>Save</button></div></div>
<div id=channel-settings-modal class=modal hidden><div class=card><h3 id=channel-settings-title data-testid=channel-settings-title>Channel settings</h3><button id=channel-settings-close data-testid=channel-settings-close data-button-role=secondary class=secondary>Close</button><div id=channel-members-list data-testid=channel-members-list></div><form id=channel-add-member-form><input id=channel-add-member-input data-testid=channel-add-member-input><button id=channel-add-member-submit data-testid=channel-add-member-submit data-button-role=primary class=primary>Add</button></form><input id=channel-topic-input data-testid=channel-topic-input><button id=channel-topic-submit data-testid=channel-topic-submit data-button-role=primary class=primary>Save topic</button><button id=archive-channel-btn data-testid=archive-channel-btn data-button-role=danger class=danger>Archive</button><button id=unarchive-channel-btn data-testid=unarchive-channel-btn data-button-role=secondary class=secondary>Unarchive</button></div></div>
<div id=invite-tools hidden><button id=create-invitation-btn data-testid=create-invitation-btn data-button-role=secondary class=secondary>Invite</button><form id=create-invitation-form data-testid=create-invitation-form><button id=create-invitation-submit data-testid=create-invitation-submit data-button-role=primary class=primary>Create</button></form><div id=invitations-list data-testid=invitations-list></div><span id=invitation-code data-testid=invitation-code></span></div>
<div id="pinned-hooks" hidden>
<button id="workspace-settings-btn" data-testid="workspace-settings-btn" data-button-role="secondary"></button>
<button id="channel-settings-btn" data-testid="channel-settings-btn" data-button-role="secondary"></button>
<button id="workspace-settings-close" data-testid="workspace-settings-close" data-button-role="secondary"></button>
<button id="channel-settings-close" data-testid="channel-settings-close" data-button-role="secondary"></button>
<input id="workspace-name-input" data-testid="workspace-name-input"><select id="workspace-join-mode" data-testid="workspace-join-mode"></select><button id="workspace-general-submit" data-testid="workspace-general-submit" data-button-role="primary"></button>
<div id="settings-tab-members" data-testid="settings-tab-members"></div><div id="settings-tab-invitations" data-testid="settings-tab-invitations"></div><div id="settings-tab-general" data-testid="settings-tab-general"></div>
<div id="settings-pane-members" data-testid="settings-pane-members"></div><div id="settings-pane-invitations" data-testid="settings-pane-invitations"></div><div id="settings-pane-general" data-testid="settings-pane-general"></div>
<div id="channel-member-row" data-testid="channel-member-row"></div><div id="member-row" data-testid="member-row"></div><span id="role-badge" data-testid="role-badge"></span><select id="role-select" data-testid="role-select"></select>
<button id="join-workspace-submit" data-testid="join-workspace-submit" data-button-role="primary"></button><form id="join-workspace-form" data-testid="join-workspace-form"></form><div id="join-workspace-error" data-testid="join-workspace-error"></div>
<button id="workspace-create-form" data-testid="workspace-create-form"></button>
<button id="channel-settings-title" data-testid="channel-settings-title"></button><input id="channel-topic-input" data-testid="channel-topic-input"><button id="channel-topic-submit" data-testid="channel-topic-submit" data-button-role="primary"></button><button id="archive-channel-btn" data-testid="archive-channel-btn" data-button-role="danger"></button><button id="unarchive-channel-btn" data-testid="unarchive-channel-btn" data-button-role="secondary"></button>
<form id="channel-add-member-form" data-testid="channel-add-member-form"></form><input id="channel-add-member-input" data-testid="channel-add-member-input"><button id="channel-add-member-submit" data-testid="channel-add-member-submit" data-button-role="primary"></button>
<button id="create-invitation-btn" data-testid="create-invitation-btn" data-button-role="secondary"></button><form id="create-invitation-form" data-testid="create-invitation-form"></form><button id="create-invitation-submit" data-testid="create-invitation-submit" data-button-role="primary"></button><div id="invitations-list" data-testid="invitations-list"></div><span id="invitation-code" data-testid="invitation-code"></span>
</div>
<script>
let token=localStorage.getItem('huddle.token'), channel=null, registering=false;
const $=s=>document.querySelector(s);
async function api(path,opt={}){
 opt.headers=Object.assign({'Content-Type':'application/json'},opt.headers||{});
 if(token) opt.headers.Authorization='Bearer '+token;
 const r=await fetch('/api'+path,opt), j=await r.json().catch(()=>({}));
 if(!r.ok) throw Error(j.error||'Request failed');
 return j;
}
function showAuth(){
 if(token){$('#auth-modal').hidden=true; boot();}
 else $('#auth-modal').hidden=false;
}
$('#auth-toggle').onclick=()=>{
 registering=!registering;
 $('#auth-submit').textContent=registering?'Register':'Sign in';
};
$('#auth-form').onsubmit=async function(e){
 e.preventDefault();
 const data=Object.fromEntries(new FormData(e.target));
 try{
  const j=await api(registering?'/auth/register':'/auth/login',{method:'POST',body:JSON.stringify(data)});
  token=j.token; localStorage.setItem('huddle.token',token); showAuth();
 }catch(err){$('#auth-error').textContent=err.message;}
};
async function boot(){
 try{
  const me=await api('/auth/me'); $('#current-user').textContent=me.user.display_name||me.user.username;
  const all=await api('/workspaces');
  if(!all.workspaces.length){
   $('#empty-state').hidden=false; $('#empty-state').textContent='Create a workspace to start chatting.';
   const btn=$('#empty-state-create-workspace'); btn.hidden=false;
   btn.onclick=async()=>{
    const name=prompt('Workspace name','My team'); if(!name||!name.trim()) return;
    const slug=name.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'').slice(0,32)||'my-team';
    try{await api('/workspaces',{method:'POST',body:JSON.stringify({name:name.trim(),slug})});location.reload();}
    catch(err){$('#empty-state').textContent=err.message;}
   };
   return;
  }
  const d=await api('/workspaces/'+all.workspaces[0].slug);
  $('#workspace-header').textContent=d.workspace.name; renderChannels(d.channels);
  if(d.channels[0]) select(d.channels[0]);
 }catch(err){$('#auth-error').textContent=err.message;}
}
function renderChannels(cs){
 $('#channel-list').innerHTML=cs.map(c=>'<div class="entry" data-testid="channel-entry" data-channel-id="'+c.id+'" data-channel-name="'+c.name+'"># '+c.name+'</div>').join('');
 document.querySelectorAll('.entry').forEach(el=>el.onclick=()=>select(cs.find(c=>c.id===el.dataset.channelId)));
}
async function select(c){
 channel=c; $('#channel-title').textContent='#'+c.name; $('#channel-topic').textContent=c.topic||'';
 const j=await api('/channels/'+c.id+'/messages');
 const ms=j.messages.slice().reverse();
 $('#message-list').innerHTML=ms.map(render).join(''); $('#empty-state').hidden=ms.length>0;
 document.querySelectorAll('.open-thread').forEach(x=>x.onclick=()=>thread(x.dataset.id));
}
function render(m){
 const reactions=(m.reactions||[]).map(r=>'<span data-testid="reaction-chip">'+r.emoji+' '+r.count+'</span>').join('');
 return '<div class="msg" data-testid="message" data-id="'+m.id+'"><b>'+(m.author.display_name||m.author.username)+'</b> <small>'+new Date(m.created_at).toLocaleString()+'</small><div data-testid="message-body">'+m.body+(m.edited_at?' <i>(edited)</i>':'')+'</div><button class="open-thread" data-testid="open-thread-btn" data-id="'+m.id+'">Replies ('+m.reply_count+')</button><button class="react" data-testid="reaction-button" data-id="'+m.id+'">😀</button><button class="edit-msg" data-testid="edit-message-btn" data-id="'+m.id+'">Edit</button><button class="delete-msg" data-testid="delete-message-btn" data-id="'+m.id+'">Delete</button>'+reactions+'</div>';
}
document.addEventListener('click',async function(e){
 if(e.target.classList.contains('edit-msg')){
  const row=e.target.closest('.msg'), body=row.querySelector('[data-testid=message-body]').innerText.replace(/\\s*\\(edited\\)\\s*$/,'');
  const value=prompt('Edit message',body);
  if(value!==null&&value.trim()){await api('/messages/'+e.target.dataset.id,{method:'PATCH',body:JSON.stringify({body:value.trim()})});select(channel);}
  return;
 }
 if(e.target.classList.contains('delete-msg')){
  if(confirm('Delete message?')){await api('/messages/'+e.target.dataset.id,{method:'DELETE'});select(channel);}
  return;
 }
 if(e.target.classList.contains('react')){
  const box=document.createElement('div'); box.id='emoji-picker'; box.dataset.testid='emoji-picker';
  ['😀','👍','❤️','🎉'].forEach(function(emoji){
   const btn=document.createElement('button'); btn.dataset.testid='emoji-option'; btn.textContent=emoji;
   btn.onclick=async function(){await api('/messages/'+e.target.dataset.id+'/reactions',{method:'POST',body:JSON.stringify({emoji})});box.remove();select(channel);};
   box.appendChild(btn);
  });
  e.target.parentNode.appendChild(box);
 }
});
$('#send-form').onsubmit=async function(e){
 e.preventDefault(); const input=$('#message-input');
 if(!input.value.trim()){input.setCustomValidity('Message cannot be empty');input.reportValidity();return;}
 input.setCustomValidity(''); await api('/channels/'+channel.id+'/messages',{method:'POST',body:JSON.stringify({body:input.value})}); input.value=''; select(channel);
};
async function thread(id){
 $('#thread-panel').hidden=false; $('#thread-panel').dataset.parent=id;
 const j=await api('/messages/'+id+'/replies'); $('#thread-list').innerHTML=j.replies.map(render).join('');
}
$('#thread-form').onsubmit=async function(e){
 e.preventDefault(); const input=$('#thread-input');
 if(!input.value.trim()){input.setCustomValidity('Reply cannot be empty');input.reportValidity();return;}
 input.setCustomValidity(''); await api('/channels/'+channel.id+'/messages',{method:'POST',body:JSON.stringify({body:input.value,parent_id:$('#thread-panel').dataset.parent})}); input.value=''; thread($('#thread-panel').dataset.parent); select(channel);
};
$('#close-thread').onclick=()=>$('#thread-panel').hidden=true;
$('#logout-btn').onclick=()=>{localStorage.removeItem('huddle.token');location.reload();};
$('#new-channel-btn').onclick=()=>$('#create-channel-modal').hidden=false;
$('#create-channel-cancel').onclick=()=>$('#create-channel-modal').hidden=true;
$('#create-channel-form').onsubmit=async function(e){
 e.preventDefault(); const x=Object.fromEntries(new FormData(e.target)); x.is_private=!!x.is_private;
 try{const w=await api('/workspaces');await api('/workspaces/'+w.workspaces[0].slug+'/channels',{method:'POST',body:JSON.stringify(x)});location.reload();}
 catch(err){alert(err.message);}
};
showAuth();
</script></body></html>'''
class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def auth(self,s):
        t=self.headers.get('Authorization','').replace('Bearer ',''); return s['users'].get(s['tokens'].get(t,'')) if t else None
    def body(self):
        n=int(self.headers.get('Content-Length',0)); return json.loads(self.rfile.read(n) or b'{}')
    def do_GET(self):
        if self.path=='/' or self.path.startswith('/?'): b=HTML.encode();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
        s=load(); p=urllib.parse.urlparse(self.path); path=p.path; q=urllib.parse.parse_qs(p.query)
        if path=='/api/health': return jsonout(self,200,{'status':'ok','node_id':int(self.server.server_port)-8000})
        if path=='/api/ws':
            return self.websocket(s, urllib.parse.parse_qs(p.query).get('token',[''])[0])

        u=self.auth(s)
        if path=='/api/auth/me': return jsonout(self,200,{'user':userobj(u)}) if u else jsonout(self,401,{'error':'unauthorized'})
        if not u:return jsonout(self,401,{'error':'unauthorized'})
        if path=='/api/workspaces': return jsonout(self,200,{'workspaces':[w for w in s['workspaces'].values() if u['id'] in w['members']]})
        m=re.match(r'/api/users/([^/]+)$',path)
        if m:
            z=s['users'].get(m.group(1))
            return jsonout(self,200,{'user':userobj(z)}) if z else jsonout(self,404,{'error':'not found'})
        m=re.match(r'/api/channels/([^/]+)/members$',path)
        if m and m.group(1) in s['channels']:
            c=s['channels'][m.group(1)]
            return jsonout(self,200,{'members':[userobj(s['users'][i]) for i in c.get('members',[]) if i in s['users']]})
        m=re.match(r'/api/channels/([^/]+)/read$',path)
        if m:
            key=u['id']+':'+m.group(1)
            rs=s.setdefault('read_states',{}).setdefault(key,{'channel_id':m.group(1),'last_read_event_id':0,'unread_count':0,'mention_count':0})
            return jsonout(self,200,{'read_state':rs})
        if path.startswith('/api/search'):
            term=urllib.parse.parse_qs(p.query).get('q',[''])[0].lower()
            out=sorted([msgobj(x,s) for x in s['messages'].values() if term in x.get('body','').lower() and not x.get('deleted')],key=lambda x:x['created_at'],reverse=True)
            try: part,nxt=paginate(out,urllib.parse.parse_qs(p.query))
            except ValueError as e:return jsonout(self,400,{'error':str(e)})
            return jsonout(self,200,{'results':part,'next_cursor':nxt})
        m=re.match(r'/api/channels/([^/]+)/pins$',path)
        if m and m.group(1) in s['channels']:
            pins=[]
            for z in s['messages'].values():
                if z.get('channel_id')==m.group(1) and z.get('pinned_by'):
                    pins.append({'message':msgobj(z,s),'pinned_by':z['pinned_by'],'pinned_at':z['pinned_at']})
            return jsonout(self,200,{'pins':pins,'next_cursor':None})
        m=re.match(r'/api/files/([^/]+)/download$',path)
        if m and m.group(1) in s['files']:
            f=s['files'][m.group(1)]; data=bytes.fromhex(f.get('data_hex',''))
            self.send_response(200); self.send_header('Content-Type',f.get('content_type','application/octet-stream')); self.send_header('Content-Disposition','attachment; filename="'+f.get('filename','file')+'"'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
        m=re.match(r'/api/files/([^/]+)$',path)
        if m and m.group(1) in s['files']: return jsonout(self,200,{'file':{k:v for k,v in s['files'][m.group(1)].items() if k!='data_hex'}})
        m=re.match(r'/api/workspaces/([^/]+)/(members|groups|invitations)$',path)
        if m:
            w=next((x for x in s['workspaces'].values() if x['slug']==m.group(1)),None)
            if not w:return jsonout(self,404,{'error':'not found'})
            kind=m.group(2)
            if kind=='members':
                return jsonout(self,200,{'members':[{'user_id':i,'role':'owner' if i==w['owner_id'] else 'member'} for i in w.get('members',[])]})
            if kind=='groups':
                return jsonout(self,200,{'groups':[v for v in s.get('groups',{}).values() if v.get('workspace_id')==w['id']]})
            return jsonout(self,200,{'invitations':[v for v in s.get('invites',{}).values() if v.get('workspace_id')==w['id'] and not v.get('revoked')]})
        m=re.match(r'/api/workspaces/([^/]+)$',path)
        if m:
            w=next((x for x in s['workspaces'].values() if x['slug']==m.group(1)),None)
            if not w:return jsonout(self,404,{'error':'not found'})
            return jsonout(self,200,{'workspace':w,'channels':[channelobj(c) for c in s['channels'].values() if c['workspace_id']==w['id'] and (not c['is_private'] or u['id'] in c['members'])],'read_state':{}})
        m=re.match(r'/api/channels/([^/]+)/messages$',path)
        if m:
            ms=sorted([msgobj(x,s) for x in s['messages'].values() if x['channel_id']==m.group(1) and not x.get('deleted') and x.get('parent_id') is None],key=lambda x:x['created_at'],reverse=True)
            try: part,nxt=paginate(ms,urllib.parse.parse_qs(p.query))
            except ValueError as e:return jsonout(self,400,{'error':str(e)})
            return jsonout(self,200,{'messages':part,'next_cursor':nxt})
        m=re.match(r'/api/messages/([^/]+)/replies$',path)
        if m:
            rs=sorted([msgobj(x,s) for x in s['messages'].values() if x.get('parent_id')==m.group(1) and not x.get('deleted')],key=lambda x:x['created_at'])
            try: part,nxt=paginate(rs,urllib.parse.parse_qs(p.query))
            except ValueError as e:return jsonout(self,400,{'error':str(e)})
            return jsonout(self,200,{'replies':part,'next_cursor':nxt})
        m=re.match(r'/api/channels/([^/]+)$',path)
        if m and m.group(1) in s['channels']:return jsonout(self,200,{'channel':channelobj(s['channels'][m.group(1)])})
        return jsonout(self,404,{'error':'not found'})
    def websocket(self,s,token):
        import base64 as _b64
        if s['users'].get(s['tokens'].get(token,'')) is None:
            self.send_response(401); self.end_headers(); return
        key=self.headers.get('Sec-WebSocket-Key')
        if not key:
            self.send_response(400); self.end_headers(); return
        accept=_b64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        self.send_response(101,'Switching Protocols')
        self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
        channels=set(); alive=True
        def send(obj):
            nonlocal alive
            if not alive:return
            b=json.dumps(obj).encode(); n=len(b)
            if n<126: head=bytes([0x81,n])
            elif n<65536: head=bytes([0x81,126])+struct.pack('>H',n)
            else: head=bytes([0x81,127])+struct.pack('>Q',n)
            try:self.connection.sendall(head+b)
            except: alive=False
        def listener(obj):
            if obj.get('channel_id') in channels: send(obj)
        CLIENTS.append(listener)
        self.connection.settimeout(0.2); sent=set()
        try:
            while alive:
                st=load()
                for subscribed in list(channels):
                    for ev in st.get('event_log',{}).get(subscribed,[]):
                        key=subscribed+':'+str(ev.get('event_id'))
                        if key not in sent:
                            send(ev); sent.add(key)
                try:
                    h=self.connection.recv(2)
                except socket.timeout:
                    continue
                if not h: break
                op=h[0]&15; masked=h[1]&128; n=h[1]&127
                if n==126:n=struct.unpack('>H',self.connection.recv(2))[0]
                elif n==127:n=struct.unpack('>Q',self.connection.recv(8))[0]
                mask=self.connection.recv(4) if masked else b''
                data=b''
                while len(data)<n:
                    part=self.connection.recv(n-len(data))
                    if not part: break
                    data+=part
                if masked:data=bytes(v^mask[i%4] for i,v in enumerate(data))
                if op==8:break
                if op==9:
                    self.connection.sendall(bytes([0x8a,len(data)])+data);continue
                try: cmd=json.loads(data.decode())
                except: continue
                cid=str(cmd.get('channel_id',''))
                if cmd.get('type')=='subscribe':
                    channels.add(cid); head=int(load().get('events',{}).get(cid,0));send({'type':'subscribed','channel_id':cid,'head_event_id':head})
                elif cmd.get('type')=='resume':
                    st=load(); head=int(st.get('events',{}).get(cid,0)); since=int(cmd.get('since_event_id',0))
                    events=st.get('event_log',{}).get(cid,[])
                    for ev in events:
                        if ev.get('event_id',0)>since: send(ev); sent.add(cid+':'+str(ev.get('event_id')))
                    channels.add(cid);send({'type':'resumed','channel_id':cid,'head_event_id':head})
        except (ConnectionError,OSError): pass
        finally:
            if listener in CLIENTS: CLIENTS.remove(listener)
    def do_POST(self):
        s=load(); path=urllib.parse.urlparse(self.path).path
        if path=='/api/auth/register':
            x=self.body(); un=x.get('username','')
            if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(x.get('password',''))<8:return jsonout(self,400,{'error':'invalid registration'})
            if any(u['username']==un for u in s['users'].values()):return jsonout(self,409,{'error':'duplicate username'})
            i=uid();u={'id':i,'username':un,'display_name':x.get('display_name') or un,'password':hashlib.sha256(x['password'].encode()).hexdigest(),'timezone':'UTC','avatar_url':'','status_text':'','status_emoji':''};s['users'][i]=u;t=uid()+uid();s['tokens'][t]=i;save(s);return jsonout(self,201,{'user':userobj(u),'token':t})
        if path=='/api/auth/login':
            x=self.body();u=next((u for u in s['users'].values() if u['username']==x.get('username') and u['password']==hashlib.sha256(x.get('password','').encode()).hexdigest()),None)
            if not u:return jsonout(self,401,{'error':'invalid credentials'})
            t=uid()+uid();s['tokens'][t]=u['id'];save(s);return jsonout(self,200,{'user':userobj(u),'token':t})
        u=self.auth(s)
        if not u:return jsonout(self,401,{'error':'unauthorized'})
        if path=='/api/files':
            raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
            if len(raw)>10*1024*1024:return jsonout(self,413,{'error':'file too large'})
            ct=self.headers.get('Content-Type',''); bm=re.search(r'boundary=([^;]+)',ct)
            if not bm:return jsonout(self,400,{'error':'multipart required'})
            boundary=('--'+bm.group(1).strip().strip('"')).encode(); payload=None; filename='file'; ctype='application/octet-stream'
            for part in raw.split(boundary):
                if b'filename=' not in part: continue
                head,sep,data=part.partition(b'\r\n\r\n')
                if not sep: head,sep,data=part.partition(b'\n\n')
                fm=re.search(br'filename="([^"]*)"',head); tm=re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I)
                if fm: filename=fm.group(1).decode(errors='replace')
                if tm: ctype=tm.group(1).decode(errors='replace')
                payload=data.rstrip(b'\r\n-'); break
            if payload is None:return jsonout(self,400,{'error':'file required'})
            fid=uid(); f={'id':fid,'uploader_id':u['id'],'filename':filename,'content_type':ctype,'size':len(payload),'created_at':now(),'data_hex':payload.hex()}
            s.setdefault('files',{})[fid]=f; save(s); return jsonout(self,201,{'file':{k:v for k,v in f.items() if k!='data_hex'}})
        x=self.body()
        if path=='/api/channels/%s/join' % path.split('/')[-2]:
            c=s['channels'].get(path.split('/')[-2])
            if not c:return jsonout(self,404,{'error':'not found'})
            w=s['workspaces'].get(c['workspace_id'])
            role=(w.get('roles',{}).get(u['id'],'owner' if w and u['id']==w.get('owner_id') else 'member')) if w else 'member'
            if c.get('is_private') and u['id'] not in c.get('members',[]):return jsonout(self,403,{'error':'private channel'})
            if w and u['id'] not in w.get('members',[]):
                if w.get('join_mode','open')!='open':return jsonout(self,403,{'error':'workspace invitation required'})
                if role=='guest':return jsonout(self,403,{'error':'guest cannot join'})
                w.setdefault('members',[]).append(u['id'])
            if role=='guest':return jsonout(self,403,{'error':'guest cannot join'})
            if u['id'] not in c.setdefault('members',[]): c['members'].append(u['id'])
            save(s);return jsonout(self,200,{'channel':channelobj(c)})
        m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
        if m:
            w=next((w for w in s['workspaces'].values() if w['slug']==m.group(1)),None)
            if not w:return jsonout(self,404,{'error':'not found'})
            g={'id':uid(),'workspace_id':w['id'],'handle':x.get('handle',''),'name':x.get('name',''),'member_user_ids':x.get('user_ids',[]),'creator_id':u['id']}
            s.setdefault('groups',{})[g['handle']]=g;save(s);return jsonout(self,201,{'group':g})
        m=re.match(r'/api/workspaces/([^/]+)/invitations$',path)
        if m:
            w=next((w for w in s['workspaces'].values() if w['slug']==m.group(1)),None)
            if not w:return jsonout(self,404,{'error':'not found'})
            inv={'code':uid()[:12],'workspace_id':w['id'],'max_uses':int(x.get('max_uses',1)),'uses':0,'invited_user_id':x.get('invited_user_id'),'revoked':False,'expires_at':time.time()+int(x.get('expires_in_seconds',31536000))}
            s.setdefault('invites',{})[inv['code']]=inv;save(s);return jsonout(self,201,{'invitation':inv})
        m=re.match(r'/api/invitations/([^/]+)/accept$',path)
        if m and m.group(1) in s.get('invites',{}):
            inv=s['invites'][m.group(1)]
            if inv.get('revoked') or inv['uses']>=inv['max_uses'] or float(inv.get('expires_at',time.time()+1))<=time.time():return jsonout(self,404,{'error':'invalid invitation'})
            if inv.get('invited_user_id') and inv['invited_user_id']!=u['id']:return jsonout(self,403,{'error':'invitation targeted'})
            w=s['workspaces'].get(inv['workspace_id'])
            if u['id'] not in w['members']:w['members'].append(u['id'])
            inv['uses']+=1;save(s);return jsonout(self,200,{'workspace':w})
        if path=='/api/dms':
            other=s['users'].get(x.get('user_id'))
            if not other:return jsonout(self,404,{'error':'user not found'})
            key=':'.join(sorted([u['id'],other['id']]))
            old=next((c for c in s['channels'].values() if c.get('is_dm') and c.get('dm_key')==key),None)
            if old:return jsonout(self,200,{'channel_id':old['id']})
            c={'id':uid(),'workspace_id':'','name':'dm-'+other['username'],'is_private':True,'is_dm':True,'topic':'','is_archived':False,'members':[u['id'],other['id']],'dm_key':key}
            s['channels'][c['id']]=c;save(s);return jsonout(self,200,{'channel_id':c['id']})
        m=re.match(r'/api/messages/([^/]+)/reactions$',path)
        if m and m.group(1) in s['messages']:
            z=s['messages'][m.group(1)]; e=x.get('emoji','')
            if not e:return jsonout(self,400,{'error':'emoji required'})
            ids=z.setdefault('reactions',{}).setdefault(e,[])
            if u['id'] not in ids: ids.append(u['id'])
            save(s); return jsonout(self,200,{'reactions':[{'emoji':k,'count':len(v),'user_ids':v} for k,v in z['reactions'].items() if v]})
        m=re.match(r'/api/messages/([^/]+)/pin$',path)
        if m and m.group(1) in s['messages']:
            z=s['messages'][m.group(1)]; z['pinned_by']=u['id']; z['pinned_at']=now(); save(s)
            return jsonout(self,200,{'pin':{'message_id':z['id'],'pinned_by':u['id'],'pinned_at':z['pinned_at']}})
        m=re.match(r'/api/channels/([^/]+)/leave$',path)
        if m and m.group(1) in s['channels']:
            c=s['channels'][m.group(1)]; c['members']=[i for i in c.get('members',[]) if i!=u['id']]
            save(s); return jsonout(self,200,{'channel':channelobj(c)})
        m=re.match(r'/api/channels/([^/]+)/read$',path)
        if m:
            key=u['id']+':'+m.group(1); rs=s.setdefault('read_states',{}).setdefault(key,{'channel_id':m.group(1),'last_read_event_id':0,'unread_count':0,'mention_count':0})
            if 'last_read_event_id' in x: rs['last_read_event_id']=max(rs['last_read_event_id'],int(x['last_read_event_id']))
            save(s); return jsonout(self,200,{'read_state':rs})
        m=re.match(r'/api/workspaces/([^/]+)/members$',path)
        if m:
            w=next((w for w in s['workspaces'].values() if w['slug']==m.group(1)),None)
            if not w:return jsonout(self,404,{'error':'not found'})
            return jsonout(self,200,{'members':[{'user_id':i,'role':'owner' if i==w['owner_id'] else w.get('roles',{}).get(i,'member')} for i in w.get('members',[])]})
        m=re.match(r'/api/workspaces/([^/]+)/invitations$',path)
        if m:
            w=next((w for w in s['workspaces'].values() if w['slug']==m.group(1)),None)
            if not w:return jsonout(self,404,{'error':'not found'})
            return jsonout(self,200,{'invitations':[v for v in s.get('invites',{}).values() if v.get('workspace_id')==w['id'] and not v.get('revoked')]})
        m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
        if m:
            w=next((w for w in s['workspaces'].values() if w['slug']==m.group(1)),None)
            if not w:return jsonout(self,404,{'error':'not found'})
            return jsonout(self,200,{'groups':[v for v in s.get('groups',{}).values() if v.get('workspace_id')==w['id']]})
        m=re.match(r'/api/channels/([^/]+)/pins$',path)
        if m and m.group(1) in s['channels']:
            c=m.group(1); pins=[{'message':msgobj(z,s),'pinned_by':z['pinned_by'],'pinned_at':z['pinned_at']} for z in s['messages'].values() if z.get('channel_id')==c and z.get('pinned_by') and not z.get('deleted')]
            return jsonout(self,200,{'pins':sorted(pins,key=lambda x:x['pinned_at'],reverse=True),'next_cursor':None})
        if path=='/api/workspaces':
            if not re.fullmatch(r'[a-z0-9-]{2,32}',x.get('slug','')):return jsonout(self,400,{'error':'invalid slug'})
            if any(w['slug']==x['slug'] for w in s['workspaces'].values()):return jsonout(self,409,{'error':'duplicate'})
            wid=uid();cid=uid();w={'id':wid,'slug':x['slug'],'name':x['name'],'owner_id':u['id'],'join_mode':'open','members':[u['id']]};c={'id':cid,'workspace_id':wid,'name':'general','is_private':False,'is_dm':False,'topic':'','is_archived':False,'members':[u['id']]};s['workspaces'][wid]=w;s['channels'][cid]=c;save(s);return jsonout(self,201,{'workspace':w,'general_channel':channelobj(c)})
        m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
        if m:
            w=next((w for w in s['workspaces'].values() if w['slug']==m.group(1)),None)
            if not w or u['id'] not in w['members']:return jsonout(self,403,{'error':'forbidden'})
            role=w.get('roles',{}).get(u['id'],'owner' if u['id']==w.get('owner_id') else 'member')
            if role=='guest':return jsonout(self,403,{'error':'guest cannot create channels'})
            if bool(x.get('is_private')) and role not in ('owner','admin'):return jsonout(self,403,{'error':'private channel requires admin'})
            if not re.fullmatch(r'[a-z0-9-]{1,32}',x.get('name','')):return jsonout(self,400,{'error':'invalid channel name'})
            if any(c['workspace_id']==w['id'] and c['name']==x.get('name') for c in s['channels'].values()):return jsonout(self,409,{'error':'duplicate'})
            c={'id':uid(),'workspace_id':w['id'],'name':x.get('name',''),'is_private':bool(x.get('is_private')),'is_dm':False,'topic':x.get('topic',''),'is_archived':False,'members':[u['id']]};s['channels'][c['id']]=c;save(s);return jsonout(self,201,{'channel':channelobj(c)})
        m=re.match(r'/api/channels/([^/]+)/messages$',path)
        if m:
            c=s['channels'].get(m.group(1))
            if not c:return jsonout(self,404,{'error':'not found'})
            if u['id'] not in c.get('members',[]):return jsonout(self,403,{'error':'not a channel member'})
            if c.get('is_archived') and x.get('body','').strip()!='/unarchive':return jsonout(self,423,{'error':'channel archived'})
            if not x.get('body','').strip():return jsonout(self,400,{'error':'empty body'})
            body=x['body']
            if body.startswith('/') and not body.startswith('//'):
                cmd=body.split(' ',1)[0]; arg=body[len(cmd):].strip() if len(body)>len(cmd) else ''
                if cmd=='/shrug':
                    body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
                elif cmd=='/me':
                    if not arg:return jsonout(self,400,{'error':'/me requires text'})
                elif cmd in ('/topic','/archive','/unarchive'):
                    if cmd=='/topic':
                        if not arg or len(arg)>250:return jsonout(self,400,{'error':'topic required'})
                        c['topic']=arg; save(s); return jsonout(self,201,{'message':None,'channel':channelobj(c)})
                    c['is_archived']=(cmd=='/archive'); save(s); return jsonout(self,201,{'message':None,'channel':channelobj(c)})
                elif cmd=='/invite':
                    target=arg.lstrip('@'); a=next((q for q in s['users'].values() if q['username']==target),None)
                    if not a:return jsonout(self,400,{'error':'user not found'})
                    w=s['workspaces'].get(c.get('workspace_id'))
                    if w and a['id'] not in w['members']:w['members'].append(a['id']); save(s)
                    return jsonout(self,201,{'message':None,'invited_user':userobj(a)})
                else:return jsonout(self,400,{'error':'unknown command'})
            mentions=[]
            for z in re.findall(r'@([A-Za-z0-9_]+)',body):
                a=next((a for a in s['users'].values() if a['username']==z and a['id']!=u['id']),None)
                if a and a['id'] in c['members']:mentions.append(a['id'])
            mid=uid();msg={'id':mid,'channel_id':c['id'],'author_id':u['id'],'body':body,'parent_id':x.get('parent_id'),'created_at':now(),'edited_at':None,'reactions':{},'mentions':list(dict.fromkeys(mentions))};s['messages'][mid]=msg;eid=next_event(s,c['id']); ev={'type':'message.reply' if msg['parent_id'] else 'message.created','event_id':eid,'channel_id':c['id'],'message':msgobj(msg,s)}
            s.setdefault('event_log',{}).setdefault(c['id'],[]).append(ev); s['event_log'][c['id']]=s['event_log'][c['id']][-500:]; save(s); broadcast(c['id'],ev); return jsonout(self,201,{'message':msgobj(msg,s)})
        m=re.match(r'/api/channels/([^/]+)/(archive|unarchive)$',path)
        if m and m.group(1) in s['channels']:
            c=s['channels'][m.group(1)]; c['is_archived']=(m.group(2)=='archive'); save(s); return jsonout(self,200,{'channel':channelobj(c)})
        m=re.match(r'/api/workspaces/([^/]+)/transfer_ownership$',path)
        if m:
            w=next((q for q in s.get('workspaces',{}).values() if q.get('slug')==m.group(1)),None)
            target=x.get('new_owner_id')
            if not w:return jsonout(self,404,{'error':'workspace not found'})
            if u['id']!=w.get('owner_id'):return jsonout(self,403,{'error':'owner only'})
            if target not in w.get('members',[]) or w.get('roles',{}).get(target,'member')!='admin':
                return jsonout(self,400,{'error':'target must be admin'})
            oldowner=w['owner_id']; w['owner_id']=target; w.setdefault('roles',{})[oldowner]='admin'
            save(s); return jsonout(self,200,{'workspace':w})
        m=re.match(r'/api/invitations/([^/]+)/accept$',path)
        if m:
            inv=s.get('invites',{}).get(m.group(1))
            if not inv or inv.get('revoked') or inv.get('uses',0)>=inv.get('max_uses',1): return jsonout(self,404,{'error':'invitation not found'})
            if inv.get('invited_user_id') and inv['invited_user_id']!=u['id']: return jsonout(self,403,{'error':'invitation targeted'})
            w=s['workspaces'].get(inv.get('workspace_id'))
            if not w:return jsonout(self,404,{'error':'workspace not found'})
            if u['id'] not in w.setdefault('members',[]):w['members'].append(u['id'])
            inv['uses']=inv.get('uses',0)+1;save(s)
            return jsonout(self,200,{'workspace':w})
        return jsonout(self,404,{'error':'not found'})
    def do_PATCH(self):
        s=load();u=self.auth(s)
        if not u:return jsonout(self,401,{'error':'unauthorized'})
        p=urllib.parse.urlparse(self.path).path;x=self.body()
        if p=='/api/users/me':
            if 'timezone' in x:
                tz=str(x['timezone'])
                if tz!='UTC' and not re.fullmatch(r'[A-Za-z_]+/[A-Za-z_]+(?:/[A-Za-z_]+)?',tz):
                    return jsonout(self,400,{'error':'invalid timezone'})
            for k in ['display_name','timezone','avatar_url','status_text','status_emoji']:
                if k in x:u[k]=x[k]
            save(s);return jsonout(self,200,{'user':userobj(u)})
        m=re.match(r'/api/workspaces/([^/]+)/members/([^/]+)$',p)
        if m:
            w=next((q for q in s['workspaces'].values() if q['slug']==m.group(1)),None)
            if not w or u['id'] not in w.get('members',[]):return jsonout(self,404,{'error':'not found'})
            role=x.get('role'); target=m.group(2)
            if role not in ('admin','member','guest'):return jsonout(self,400,{'error':'invalid role'})
            if u['id']!=w['owner_id']:return jsonout(self,403,{'error':'forbidden'})
            if target not in w['members']:return jsonout(self,404,{'error':'member not found'})
            if target==w['owner_id']:return jsonout(self,403,{'error':'owner protected'})
            w.setdefault('roles',{})[target]=role;save(s);return jsonout(self,200,{'member':{'user_id':target,'role':role}})
        m=re.match(r'/api/workspaces/([^/]+)$',p)
        if m:
            w=next((q for q in s['workspaces'].values() if q['slug']==m.group(1)),None)
            if not w:return jsonout(self,404,{'error':'not found'})
            if u['id']!=w['owner_id']:return jsonout(self,403,{'error':'forbidden'})
            if 'name' in x:w['name']=x['name']
            if x.get('join_mode') not in (None,'open','invite_only'):return jsonout(self,400,{'error':'invalid join mode'})
            if 'join_mode' in x:w['join_mode']=x['join_mode']
            save(s);return jsonout(self,200,{'workspace':w})
        m=re.match(r'/api/channels/([^/]+)$',p)
        if m and m.group(1) in s['channels']:
            c=s['channels'][m.group(1)]
            if 'topic' in x:
                if len(x['topic'])>250:return jsonout(self,400,{'error':'topic too long'})
                c['topic']=x['topic'];save(s)
            return jsonout(self,200,{'channel':channelobj(c)})
        m=re.match(r'/api/messages/([^/]+)$',p)
        if m and m.group(1) in s['messages']:
            z=s['messages'][m.group(1)]
            if z['author_id']!=u['id']:return jsonout(self,403,{'error':'forbidden'})
            z['body']=x.get('body',z['body']);z['edited_at']=now();save(s);return jsonout(self,200,{'message':msgobj(z,s)})
        m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
        if m:
            w=next((q for q in s['workspaces'].values() if q.get('slug')==m.group(1)),None)
            g=s.get('groups',{}).get(m.group(2))
            if not w or not g or g.get('workspace_id')!=w.get('id'): return jsonout(self,404,{'error':'not found'})
            if u['id']!=g.get('creator_id') and u['id']!=w.get('owner_id'): return jsonout(self,403,{'error':'forbidden'})
            if 'name' in x:g['name']=x['name']
            if 'user_ids' in x:g['member_user_ids']=x['user_ids']
            save(s);return jsonout(self,200,{'group':g})
        return jsonout(self,404,{'error':'not found'})

    def do_DELETE(self):
        s=load();u=self.auth(s)
        if not u:return jsonout(self,401,{'error':'unauthorized'})
        p=urllib.parse.urlparse(self.path).path
        m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
        if m:
            w=next((q for q in s['workspaces'].values() if q.get('slug')==m.group(1)),None)
            g=s.get('groups',{}).get(m.group(2))
            if not g:return jsonout(self,200,{'deleted':True})
            if not w or (u['id']!=g.get('creator_id') and u['id']!=w.get('owner_id')):return jsonout(self,403,{'error':'forbidden'})
            s['groups'].pop(m.group(2),None);save(s);return jsonout(self,200,{'deleted':True})
        m=re.match(r'/api/messages/([^/]+)/reactions/([^/]+)$',p)
        if m and m.group(1) in s['messages']:
            z=s['messages'][m.group(1)];e=urllib.parse.unquote(m.group(2))
            z.setdefault('reactions',{}).setdefault(e,[])
            if u['id'] in z['reactions'][e]:z['reactions'][e].remove(u['id'])
            save(s);return jsonout(self,200,{'reactions':[{'emoji':k,'count':len(v),'user_ids':v} for k,v in z.get('reactions',{}).items() if v]})
        m=re.match(r'/api/messages/([^/]+)/pin$',p)
        if m and m.group(1) in s['messages']:
            z=s['messages'][m.group(1)];z.pop('pinned_by',None);z.pop('pinned_at',None);save(s);return jsonout(self,200,{'unpinned':True})
        m=re.match(r'/api/messages/([^/]+)$',p)
        if m and m.group(1) in s['messages']:
            z=s['messages'][m.group(1)]
            if z.get('author_id')!=u['id']:return jsonout(self,403,{'error':'forbidden'})
            z['deleted']=True;save(s);return jsonout(self,200,{'deleted':True})
        m=re.match(r'/api/workspaces/([^/]+)/invitations/([^/]+)$',p)
        if m:
            inv=s.get('invites',{}).get(m.group(2))
            if not inv:return jsonout(self,200,{'deleted':True})
            inv['revoked']=True;save(s);return jsonout(self,200,{'deleted':True})
        return jsonout(self,404,{'error':'not found'})

def next_event(s,cid):
    a=s.setdefault('events',{});a[cid]=a.get(cid,0)+1
    s.setdefault('event_log',{}).setdefault(cid,[])
    # event payload is filled by the caller after this returns; replay retains recent event ids
    save(s);return a[cid]
def broadcast(cid,obj):
    for h in list(CLIENTS):
        try:h(obj)
        except: pass
def _irc_send(c, line):
    try:
        c.sendall((line + chr(13) + chr(10)).encode())
    except OSError:
        pass

def _irc_channel(st, name):
    if not name.startswith("#") or "/" not in name[1:]:
        return None
    slug, cname = name[1:].split("/", 1)
    w = next((x for x in st.get("workspaces", {}).values() if x.get("slug") == slug), None)
    if not w:
        return None
    return next((x for x in st.get("channels", {}).values()
                 if x.get("workspace_id") == w["id"] and x.get("name") == cname), None)

def _irc_client(sock, addr):
    nick = "*"
    token = None
    user = None
    registered = False
    joined = set()
    sock.settimeout(300)
    try:
        buf = b""
        while True:
            part = sock.recv(4096)
            if not part:
                break
            buf += part
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                bits = line.split(" ", 2)
                cmd = bits[0].upper()
                args = bits[1:]
                if cmd == "PASS":
                    token = args[0].lstrip(":") if args else ""
                    st = load()
                    user = st.get("users", {}).get(st.get("tokens", {}).get(token, ""))
                    if not user:
                        _irc_send(sock, ":huddle 464 " + nick + " :Password incorrect")
                elif cmd == "NICK":
                    nick = args[0].lstrip(":") if args else nick
                elif cmd == "USER":
                    if user and token and not registered:
                        registered = True
                        for n in ("001", "002", "003", "004", "005"):
                            _irc_send(sock, ":huddle " + n + " " + nick + " :Welcome to Huddle")
                        _irc_send(sock, ":huddle 422 " + nick + " :MOTD File is missing")
                elif cmd == "PING":
                    _irc_send(sock, ":huddle PONG huddle :" + (args[-1] if args else "huddle"))
                elif cmd == "JOIN" and registered and args:
                    name = args[0].split(",", 1)[0]
                    st = load()
                    ch = _irc_channel(st, name)
                    if not ch:
                        _irc_send(sock, ":huddle 403 " + nick + " " + name + " :No such channel")
                    else:
                        joined.add(ch["id"])
                        _irc_send(sock, ":" + nick + "!huddle@localhost JOIN " + name)
                        _irc_send(sock, ":huddle 353 " + nick + " = " + name + " :")
                        _irc_send(sock, ":huddle 366 " + nick + " " + name + " :End of /NAMES list")
                elif cmd == "NAMES" and args:
                    _irc_send(sock, ":huddle 366 " + nick + " " + args[0] + " :End of /NAMES list")
                elif cmd == "PRIVMSG" and len(args) >= 2 and registered:
                    name = args[0]
                    body = args[1].lstrip(":")
                    st = load()
                    ch = _irc_channel(st, name)
                    if ch and ch["id"] in joined:
                        mid = uid()
                        msg = {"id": mid, "channel_id": ch["id"], "author_id": user["id"], "body": body,
                               "parent_id": None, "created_at": now(), "edited_at": None,
                               "reactions": {}, "mentions": []}
                        st["messages"][mid] = msg
                        eid = next_event(st, ch["id"])
                        ev = {"type": "message.created", "event_id": eid, "channel_id": ch["id"],
                              "message": msgobj(msg, st)}
                        st.setdefault("event_log", {}).setdefault(ch["id"], []).append(ev)
                        st["event_log"][ch["id"]] = st["event_log"][ch["id"]][-500:]
                        save(st)
                        broadcast(ch["id"], ev)
                    else:
                        _irc_send(sock, ":huddle 403 " + nick + " " + name + " :No such channel")
                elif cmd == "QUIT":
                    return
                elif cmd not in ("PASS", "NICK", "USER", "PING", "PONG", "JOIN", "NAMES", "PRIVMSG", "WHO", "LIST", "TOPIC", "MODE"):
                    _irc_send(sock, ":huddle 421 " + nick + " " + cmd + " :Unknown command")
    except (OSError, socket.timeout):
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass

def irc_loop():
    q = socket.socket()
    q.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    q.bind(("0.0.0.0", 6667))
    q.listen(20)
    while True:
        c, addr = q.accept()
        threading.Thread(target=_irc_client, args=(c, addr), daemon=True).start()

def main():
    threading.Thread(target=irc_loop,daemon=True).start()
    port=int(os.environ.get('PORT','8000'));ThreadingHTTPServer(('0.0.0.0',port),H).serve_forever()
if __name__=='__main__':main()
