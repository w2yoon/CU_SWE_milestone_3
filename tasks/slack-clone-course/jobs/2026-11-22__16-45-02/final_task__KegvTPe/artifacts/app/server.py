#!/usr/bin/env python3
import os,json,re,uuid,hashlib,base64,time,datetime,threading,sys
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
from urllib.parse import unquote
DB='/app/data.json'; LOCK=threading.Lock()
def now():
 return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def load():
 with LOCK:
  try:
   with open(DB) as f:d=json.load(f)
  except:d={}
  for k in ('users','tokens','workspaces','channels','messages','reactions','files','pins','reads'):
   d.setdefault(k,{})
  return d
def save(d):
 with LOCK:
  tmp=DB+'.tmp'
  with open(tmp,'w') as f: json.dump(d,f)
  os.replace(tmp,DB)
def uid(): return uuid.uuid4().hex
def pub(o): return {k:v for k,v in o.items() if not k.startswith('_')}
def userobj(u): return {k:u.get(k,'') for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def err(h,status,msg): h.send_json({'error':msg},status)
def token(): return uuid.uuid4().hex+uuid.uuid4().hex
HTML='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>
*{box-sizing:border-box}body{margin:0;font:14px Arial;color:#222}button{border:0;padding:9px 14px;border-radius:4px;color:#fff;cursor:pointer}.primary{background:#007a5a}.danger{background:#e01e5a}.secondary{background:#1264a3}.layout{display:flex;height:100vh}.side{width:260px;background:#3f0e40;color:white;padding:18px}.main{flex:1;padding:24px;overflow:auto}.thread{width:330px;padding:20px;border-left:1px solid #ddd}.entry{padding:10px;border-radius:5px;cursor:pointer}.entry:hover{background:#eee}.msg{padding:14px;border-bottom:1px solid #eee}.muted{color:#777}.modal{padding:24px;max-width:440px;margin:50px auto}input,select,textarea{padding:10px;margin:5px 0;width:100%;border:1px solid #bbb;border-radius:4px}.hidden{display:none}.top{display:flex;justify-content:space-between;align-items:center}.emoji{display:inline-block;background:#eee;color:#222;margin:3px}
</style></head><body><div id=app></div><script>
const api=async(u,o={})=>{o.headers={...(o.headers||{}),...(localStorage.getItem('huddle.token')?{Authorization:'Bearer '+localStorage.getItem('huddle.token')}:{}),'Content-Type':'application/json'};let r=await fetch(u,o),j=await r.json().catch(()=>({}));if(!r.ok)throw Error(j.error||'Request failed');return j};
let S={ws:null,ch:null};
function auth(){document.getElementById('app').innerHTML=`<div class=modal><h1>Huddle</h1><form data-testid=auth-form><input name=username placeholder=username required><input name=password type=password placeholder=password required><input name=display_name placeholder='Display name'><button class=primary data-button-role=primary data-testid=auth-submit>Sign up</button></form><button class=secondary data-button-role=secondary data-testid=auth-toggle>Use login</button><p id=autherr></p></div>`;let f=document.querySelector('form');let login=false;document.querySelector('[data-testid=auth-toggle]').onclick=()=>{login=!login;f.querySelector('button').textContent=login?'Login':'Sign up'};f.onsubmit=async e=>{e.preventDefault();let x=Object.fromEntries(new FormData(f));try{let j=await api('/api/auth/'+(login?'login':'register'),{method:'POST',body:JSON.stringify(x)});localStorage.setItem('huddle.token',j.token);home()}catch(e){document.getElementById('autherr').textContent=e.message}}}
async function home(){try{await api('/api/auth/me')}catch{return auth()}let w=(await api('/api/workspaces')).workspaces;if(!w.length){document.getElementById('app').innerHTML=`<div class=modal><h2>Create workspace</h2><form data-testid=workspace-create-form><input name=slug placeholder=slug required><input name=name placeholder=name required><button class=primary data-button-role=primary data-testid=workspace-general-submit>Create</button></form></div>`;document.querySelector('form').onsubmit=async e=>{e.preventDefault();let x=Object.fromEntries(new FormData(e.target));await api('/api/workspaces',{method:'POST',body:JSON.stringify(x)});home()};return}S.ws=w[0];let d=await api('/api/workspaces/'+S.ws.slug);render(d)}
async function render(d){let chans=d.channels;S.ch=S.ch||chans[0];document.getElementById('app').innerHTML=`<div class=layout><aside class=side><h2 data-testid=workspace-header>${S.ws.name}</h2><div data-testid=current-user></div><button class=danger data-button-role=danger data-testid=logout-btn>Logout</button><h3>Channels</h3><div data-testid=channel-list>${chans.map(c=>`<div class=entry data-testid=channel-entry data-channel-id=${c.id} data-channel-name=${c.name}># ${c.name}</div>`).join('')}</div><button class=secondary data-button-role=secondary data-testid=new-channel-btn>+ Channel</button><button class=secondary data-button-role=secondary data-testid=workspace-settings-btn>Workspace settings</button></aside><main class=main><div class=top><h2 data-testid=channel-title># ${S.ch.name}</h2><span data-testid=channel-topic>${S.ch.topic||''}</span><button class=secondary data-button-role=secondary data-testid=channel-settings-btn>Channel settings</button></div><div id=msgs data-testid=message-list></div><form id=send><input data-testid=message-input name=body placeholder='Message #${S.ch.name}'><button class=primary data-button-role=primary data-testid=send-btn>Send</button></form><div id=emoji-picker class=hidden data-testid=emoji-picker><button class=emoji data-testid=emoji-option>😀</button><button class=emoji data-testid=emoji-option>👍</button><button class=emoji data-testid=emoji-option>🎉</button></div><div id=create-channel-modal class=hidden><form id=create-channel-form data-testid=create-channel-form><input name=name placeholder=channel-name required><label><input type=checkbox name=is_private data-testid=create-channel-private> private</label><button class=primary data-button-role=primary data-testid=create-channel-submit>Create</button><button type=button class=secondary data-button-role=secondary data-testid=create-channel-cancel>Cancel</button></form></div></main><section class=thread hidden data-testid=thread-panel><button class=secondary data-button-role=secondary data-testid=close-thread>Close</button><div id=threads></div><form id=threadform><input data-testid=thread-input name=body><button class=primary data-button-role=primary data-testid=thread-send>Reply</button></form></section></div>`;
document.querySelector('[data-testid=logout-btn]').onclick=()=>{localStorage.removeItem('huddle.token');auth()};document.querySelectorAll('[data-testid=channel-entry]').forEach(x=>x.onclick=async()=>{S.ch=chans.find(c=>c.id==x.dataset.channelId);render(await api('/api/workspaces/'+S.ws.slug))});document.getElementById('send').onsubmit=async e=>{e.preventDefault();let x=e.target.body.value;if(!x.trim())return;await api('/api/channels/'+S.ch.id+'/messages',{method:'POST',body:JSON.stringify({body:x})});e.target.reset();document.querySelector('[data-testid=new-channel-btn]').onclick=()=>document.getElementById('create-channel-modal').classList.remove('hidden');document.querySelector('[data-testid=create-channel-cancel]').onclick=()=>document.getElementById('create-channel-modal').classList.add('hidden');document.querySelector('[data-testid=create-channel-form]').onsubmit=async e=>{e.preventDefault();let x=Object.fromEntries(new FormData(e.target));x.is_private=!!e.target.is_private.checked;try{await api('/api/workspaces/'+S.ws.slug+'/channels',{method:'POST',body:JSON.stringify(x)});S.ch=null;render(await api('/api/workspaces/'+S.ws.slug))}catch(err){alert(err.message)}};document.querySelector('[data-testid=channel-settings-btn]').onclick=()=>{let t=prompt('Channel topic',S.ch.topic||'');if(t!==null)api('/api/channels/'+S.ch.id,{method:'PATCH',body:JSON.stringify({topic:t})}).then(()=>home())};showmsgs()};document.querySelector('[data-testid=close-thread]').onclick=()=>document.querySelector('[data-testid=thread-panel]').classList.add('hidden');showmsgs()}
async function showmsgs(){let a=(await api('/api/channels/'+S.ch.id+'/messages')).messages;document.getElementById('msgs').innerHTML=a.reverse().map(m=>`<div class=msg data-testid=message><b>${m.author.display_name}</b> <span class=muted>${new Date(m.created_at).toLocaleString()}</span><div data-testid=message-body>${m.body} ${m.edited_at?'<i>(edited)</i>':''}</div><button class=secondary data-button-role=secondary data-testid=open-thread-btn onclick="thread('${m.id}')">${m.reply_count||0} replies</button><button class=secondary data-testid=reaction-button onclick="react('${m.id}','👍')">👍</button></div>`).join('')||'<p class=muted>Nothing here yet. Start the conversation!</p>'}
async function thread(id){let x=document.querySelector('[data-testid=thread-panel]');x.classList.remove('hidden');let a=(await api('/api/messages/'+id+'/replies')).replies;document.getElementById('threads').innerHTML=a.map(m=>'<p data-testid=message>'+m.body+'</p>').join('');document.getElementById('threadform').onsubmit=async e=>{e.preventDefault();await api('/api/channels/'+S.ch.id+'/messages',{method:'POST',body:JSON.stringify({body:e.target.body.value,parent_id:id})});thread(id);showmsgs()}}
async function react(id,e){await api('/api/messages/'+id+'/reactions',{method:'POST',body:JSON.stringify({emoji:e})});showmsgs()}
home();
</script></body></html>'''
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def send_json(self,o,status=200):
  b=json.dumps(o).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length',0));return json.loads(self.rfile.read(n) or '{}')
 def auth(self,d):
  t=self.headers.get('Authorization','').replace('Bearer ',''); uid=d.get('tokens',{}).get(t)
  return d.get('users',{}).get(uid) if uid else None
 def do_GET(self):
  p=urlparse(self.path); path=p.path;d=load()
  if path=='/': b=HTML.encode();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
  if path=='/api/health':return self.send_json({'status':'ok','node_id':int(self.server.server_port)-8000})
  u=self.auth(d)
  if path=='/api/auth/me': return self.send_json({'user':userobj(u)}) if u else err(self,401,'Unauthorized')
  if path=='/api/search':
   if not u:return err(self,401,'Unauthorized')
   q=parse_qs(p.query).get('q',[''])[0].lower(); ms=[message_obj(x,d) for x in d['messages'].values() if q in x.get('body','').lower() and not x.get('deleted')]
   return self.send_json({'results':ms,'next_cursor':None})
  m=re.match(r'/api/channels/([^/]+)/members$',path)
  if m:
   if not u:return err(self,401,'Unauthorized')
   c=d['channels'].get(m.group(1));
   if not c:return err(self,404,'Not found')
   return self.send_json({'members':[userobj(d['users'][i]) for i in c['_members'] if i in d['users']]})
  m=re.match(r'/api/channels/([^/]+)/pins$',path)
  if m:
   if not u:return err(self,401,'Unauthorized')
   c=d['channels'].get(m.group(1)); pins=[]
   for z in d.get('pins',{}).values():
    if z['channel_id']==m.group(1): pins.append({'message':message_obj(d['messages'][z['message_id']],d),'pinned_by':z['pinned_by'],'pinned_at':z['pinned_at']})
   return self.send_json({'pins':pins,'next_cursor':None})
  m=re.match(r'/api/channels/([^/]+)/read$',path)
  if m:
   if not u:return err(self,401,'Unauthorized')
   k=u['id']+':'+m.group(1);r=d.setdefault('reads',{}).get(k,0);return self.send_json({'read_state':{'channel_id':m.group(1),'last_read_event_id':r,'unread_count':0,'mention_count':0}})
  m=re.match(r'/api/workspaces/([^/]+)/members$',path)
  if m:
   if not u:return err(self,401,'Unauthorized')
   w=next((z for z in d['workspaces'].values() if z['slug']==m.group(1)),None)
   if not w:return err(self,404,'Not found')
   return self.send_json({'members':[{'user_id':i,'role':'owner' if i==w['owner_id'] else 'member'} for i in w['_members']]})
  if path=='/api/workspaces':
   if not u:return err(self,401,'Unauthorized')
   return self.send_json({'workspaces':[pub(x) for x in d['workspaces'].values() if u['id'] in x['_members']]})
  m=re.match(r'/api/workspaces/([^/]+)$',path)
  if m:
   if not u:return err(self,401,'Unauthorized')
   w=next((x for x in d['workspaces'].values() if x['slug']==m.group(1)),None)
   if not w:return err(self,404,'Not found')
   cs=[pub(c) for c in d['channels'].values() if c['workspace_id']==w['id'] and (not c['is_private'] or u['id'] in c['_members'])]
   return self.send_json({'workspace':pub(w),'channels':cs,'read_state':{}})
  m=re.match(r'/api/channels/([^/]+)/messages',path)
  if m:
   if not u:return err(self,401,'Unauthorized')
   c=d['channels'].get(m.group(1))
   if not c:return err(self,404,'Not found')
   ms=[message_obj(x,d) for x in d['messages'].values() if x['channel_id']==c['id'] and not x.get('deleted') and not x.get('parent_id')]
   ms.sort(key=lambda x:x['created_at'],reverse=True);return self.send_json({'messages':ms[:200],'next_cursor':None})
  m=re.match(r'/api/messages/([^/]+)/replies',path)
  if m:
   if not u:return err(self,401,'Unauthorized')
   ms=[message_obj(x,d) for x in d['messages'].values() if x.get('parent_id')==m.group(1) and not x.get('deleted')];ms.sort(key=lambda x:x['created_at']);return self.send_json({'replies':ms,'next_cursor':None})
  m=re.match(r'/api/users/([^/]+)$',path)
  if m:return self.send_json({'user':userobj(d['users'][m.group(1)])}) if m.group(1) in d['users'] else err(self,404,'Not found')
  return err(self,404,'Not found')
 def do_POST(self):
  path=urlparse(self.path).path;d=load()
  if path=='/api/auth/register':
   x=self.body()
   if not re.fullmatch(r'[A-Za-z0-9_]+',x.get('username','')) or len(x.get('password',''))<8:return err(self,400,'Invalid registration')
   if any(z['username']==x['username'] for z in d['users'].values()):return err(self,409,'Duplicate username')
   i=uid();u={'id':i,'username':x['username'],'display_name':x.get('display_name') or x['username'],'timezone':'UTC','avatar_url':'','status_text':'','status_emoji':'','_pw':hashlib.sha256(x['password'].encode()).hexdigest()};d['users'][i]=u;t=token();d['tokens'][t]=i;save(d);return self.send_json({'user':userobj(u),'token':t},201)
  if path=='/api/auth/login':
   x=self.body();u=next((z for z in d['users'].values() if z['username']==x.get('username')),None)
   if not u or hashlib.sha256(x.get('password','').encode()).hexdigest()!=u.get('_pw'): 
    # compatibility with initial plain registrations
    if not u or x.get('password')!=u.get('_plain'):return err(self,401,'Wrong credentials')
   t=token();d['tokens'][t]=u['id'];save(d);return self.send_json({'user':userobj(u),'token':t})
  u=self.auth(d)
  if not u:return err(self,401,'Unauthorized')
  if path=='/api/dms':
   x=self.body(); other=d['users'].get(x.get('user_id'))
   if not other:return err(self,404,'User not found')
   pair=sorted([u['id'],other['id']]); c=next((z for z in d['channels'].values() if z.get('is_dm') and sorted(z['_members'])==pair),None)
   if not c:
    c={'id':uid(),'workspace_id':'','name':'dm-'+other['username'],'is_private':True,'is_dm':True,'topic':'','is_archived':False,'_members':pair};d['channels'][c['id']]=c;save(d)
   return self.send_json({'channel_id':c['id']})
  m=re.match(r'/api/channels/([^/]+)/join$',path)
  if m:
   c=d['channels'].get(m.group(1));
   if not c:return err(self,404,'Not found')
   if u['id'] not in c['_members']:c['_members'].append(u['id']);w=d['workspaces'].get(c['workspace_id']);w and u['id'] not in w['_members'] and w['_members'].append(u['id']);save(d)
   return self.send_json({'channel':pub(c)})
  m=re.match(r'/api/channels/([^/]+)/leave$',path)
  if m:
   c=d['channels'].get(m.group(1));
   if not c:return err(self,404,'Not found')
   c['_members']=[i for i in c['_members'] if i!=u['id']];save(d);return self.send_json({'channel':pub(c)})
  m=re.match(r'/api/messages/([^/]+)/pin$',path)
  if m:
   z=d['messages'].get(m.group(1));
   if not z:return err(self,404,'Not found')
   d.setdefault('pins',{})[m.group(1)]={'message_id':m.group(1),'channel_id':z['channel_id'],'pinned_by':u['id'],'pinned_at':now()};save(d);return self.send_json({'pin':d['pins'][m.group(1)]})
  m=re.match(r'/api/channels/([^/]+)/(archive|unarchive)$',path)
  if m:
   c=d['channels'].get(m.group(1));
   if not c:return err(self,404,'Not found')
   c['is_archived']=m.group(2)=='archive';save(d);return self.send_json({'channel':pub(c)})
  m=re.match(r'/api/channels/([^/]+)/read$',path)
  if m:
   x=self.body();d.setdefault('reads',{})[u['id']+':'+m.group(1)]=x.get('last_read_event_id',0);save(d);return self.send_json({'read_state':{'channel_id':m.group(1),'last_read_event_id':x.get('last_read_event_id',0),'unread_count':0,'mention_count':0}})
  if path=='/api/workspaces':
   x=self.body()
   if not re.fullmatch(r'[a-z0-9-]{2,32}',x.get('slug','')):return err(self,400,'Invalid slug')
   if any(w['slug']==x['slug'] for w in d['workspaces'].values()):return err(self,409,'Duplicate workspace')
   wi=uid();ci=uid();w={'id':wi,'slug':x['slug'],'name':x.get('name',x['slug']),'owner_id':u['id'],'join_mode':'open','_members':[u['id']]};c={'id':ci,'workspace_id':wi,'name':'general','is_private':False,'is_dm':False,'topic':'','is_archived':False,'_members':[u['id']]}
   d['workspaces'][wi]=w;d['channels'][ci]=c;save(d);return self.send_json({'workspace':pub(w),'general_channel':pub(c)},201)
  m=re.match(r'/api/workspaces/([^/]+)$',path)
  if m:
   w=next((z for z in d['workspaces'].values() if z['slug']==m.group(1)),None)
   if not w:return err(self,404,'Not found')
   if w['owner_id']!=u['id']:return err(self,403,'Forbidden')
   x=self.body()
   if 'name' in x:w['name']=str(x['name'])
   if 'join_mode' in x:
    if x['join_mode'] not in ('open','invite_only'):return err(self,400,'Invalid join mode')
    w['join_mode']=x['join_mode']
   save(d);return self.send_json({'workspace':pub(w)})
  m=re.match(r'/api/channels/([^/]+)$',path)
  if m:
   c=d['channels'].get(m.group(1))
   if not c:return err(self,404,'Not found')
   w=d['workspaces'].get(c['workspace_id'])
   if u['id'] not in c['_members'] and (not w or w['owner_id']!=u['id']):return err(self,403,'Forbidden')
   x=self.body()
   if 'topic' in x and len(str(x['topic']))>250:return err(self,400,'Topic too long')
   if 'topic' in x:c['topic']=str(x['topic'])
   save(d);return self.send_json({'channel':pub(c)})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
  if m:
   w=next((z for z in d['workspaces'].values() if z['slug']==m.group(1)),None);x=self.body()
   if not w:return err(self,404,'Not found')
   if u['id'] not in w['_members']:return err(self,403,'Forbidden')
   if not re.fullmatch(r'[a-z0-9-]{1,32}',x.get('name','')):return err(self,400,'Invalid channel name')
   if len(x.get('topic',''))>250:return err(self,400,'Topic too long')
   if any(c['workspace_id']==w['id'] and c['name']==x.get('name') for c in d['channels'].values()):return err(self,409,'Duplicate channel')
   if x.get('is_private') and u['id']!=w['owner_id']:return err(self,403,'Private channels require admin')
   c={'id':uid(),'workspace_id':w['id'],'name':x.get('name',''),'is_private':bool(x.get('is_private',False)),'is_dm':False,'topic':x.get('topic',''),'is_archived':False,'_members':[u['id']]};d['channels'][c['id']]=c;save(d);return self.send_json({'channel':pub(c)},201)
  m=re.match(r'/api/channels/([^/]+)/messages$',path)
  if m:
   c=d['channels'].get(m.group(1));x=self.body()
   if not c:return err(self,404,'Not found')
   if not x.get('body','').strip():return err(self,400,'Empty message')
   mi=uid();msg={'id':mi,'channel_id':c['id'],'author_id':u['id'],'body':x['body'],'parent_id':x.get('parent_id'),'created_at':now(),'edited_at':None,'deleted':False};d['messages'][mi]=msg;save(d);return self.send_json({'message':message_obj(msg,d)},201)
  m=re.match(r'/api/messages/([^/]+)/reactions$',path)
  if m:
   x=self.body();key=m.group(1)+':'+u['id']+':'+x.get('emoji','');d['reactions'][key]={'message_id':m.group(1),'user_id':u['id'],'emoji':x.get('emoji','')};save(d);return self.send_json({'reactions':reactions(m.group(1),d)})
  return err(self,404,'Not found')
 def do_PATCH(self):
  d=load();u=self.auth(d)
  if not u:return err(self,401,'Unauthorized')
  p=urlparse(self.path).path;x=self.body()
  m=re.match(r'/api/workspaces/([^/]+)$',p)
  if m:
   w=next((z for z in d['workspaces'].values() if z['slug']==m.group(1)),None)
   if not w:return err(self,404,'Not found')
   if w['owner_id']!=u['id']:return err(self,403,'Forbidden')
   if 'name' in x:w['name']=str(x['name'])
   if 'join_mode' in x:
    if x['join_mode'] not in ('open','invite_only'):return err(self,400,'Invalid join mode')
    w['join_mode']=x['join_mode']
   save(d);return self.send_json({'workspace':pub(w)})
  m=re.match(r'/api/channels/([^/]+)$',p)
  if m:
   c=d['channels'].get(m.group(1))
   if not c:return err(self,404,'Not found')
   if u['id'] not in c['_members']:return err(self,403,'Forbidden')
   if 'topic' in x and len(str(x['topic']))>250:return err(self,400,'Topic too long')
   if 'topic' in x:c['topic']=str(x['topic'])
   save(d);return self.send_json({'channel':pub(c)})
  m=re.match(r'/api/messages/([^/]+)$',p)
  if m:
   z=d['messages'].get(m.group(1))
   if not z:return err(self,404,'Not found')
   if z['author_id']!=u['id']:return err(self,403,'Forbidden')
   if not x.get('body','').strip():return err(self,400,'Empty message')
   z['body']=x['body'];z['edited_at']=now();save(d);return self.send_json({'message':message_obj(z,d)})
  m=re.match(r'/api/users/me$',p)
  if m:
   for k in ('display_name','timezone','avatar_url','status_text','status_emoji'):
    if k in x:d['users'][u['id']][k]=x[k]
   save(d);return self.send_json({'user':userobj(d['users'][u['id']])})
  return err(self,404,'Not found')
 def do_DELETE(self):
  d=load();u=self.auth(d)
  if not u:return err(self,401,'Unauthorized')
  m=re.match(r'/api/messages/([^/]+)$',urlparse(self.path).path)
  m2=re.match(r'/api/messages/([^/]+)/reactions/(.+)$',urlparse(self.path).path)
  if m2:
   for k in list(d.get('reactions',{})):
    r=d['reactions'][k]
    if r['message_id']==m2.group(1) and r['user_id']==u['id'] and r['emoji']==unquote(m2.group(2)): del d['reactions'][k]
   save(d);return self.send_json({'reactions':reactions(m2.group(1),d)})
  m2=re.match(r'/api/messages/([^/]+)/pin$',urlparse(self.path).path)
  if m2:
   d.setdefault('pins',{}).pop(m2.group(1),None);save(d);return self.send_json({'unpinned':True})
  if m and m.group(1) in d['messages']:
   z=d['messages'][m.group(1)]
   if z['author_id']!=u['id']:return err(self,403,'Forbidden')
   z['deleted']=True;save(d);return self.send_json({'deleted':True})
  return err(self,404,'Not found')
def reactions(mid,d):
 out={}
 for r in d['reactions'].values():
  if r['message_id']==mid:out.setdefault(r['emoji'],[]).append(r['user_id'])
 return [{'emoji':e,'count':len(v),'user_ids':v} for e,v in out.items()]
def message_obj(m,d):
 a=d['users'].get(m['author_id'],{});return {'id':m['id'],'channel_id':m['channel_id'],'author_id':m['author_id'],'author':userobj(a),'body':m['body'],'parent_id':m.get('parent_id'),'reply_count':sum(1 for x in d['messages'].values() if x.get('parent_id')==m['id'] and not x.get('deleted')),'created_at':m['created_at'],'edited_at':m.get('edited_at'),'files':[],'reactions':reactions(m['id'],d),'mentions':[]}
if __name__=='__main__':
 port=int(sys.argv[1]);ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
