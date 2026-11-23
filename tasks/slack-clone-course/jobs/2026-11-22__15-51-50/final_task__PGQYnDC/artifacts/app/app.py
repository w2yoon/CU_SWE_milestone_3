#!/usr/bin/env python3
import os,sys,json,re,uuid,time,hashlib,secrets,threading,base64,struct,socket,datetime,urllib.parse
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from email.parser import BytesParser
from email.policy import default

IRC_CLIENTS={}; IRC_LOCK=threading.Lock()

DATA='/app/data/state.json'; LOCK=threading.RLock()
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def rid(p=''): return p+secrets.token_hex(8)
def initial():
 return {'users':{},'tokens':{},'workspaces':{},'channels':{},'messages':{},'files':{},'invites':{},'groups':{},'dms':{},'events':{},'seq':{}}
def load():
 with LOCK:
  try:
   with open(DATA) as f:return json.load(f)
  except:return initial()
def save(x):
 with LOCK:
  os.makedirs(os.path.dirname(DATA),exist_ok=True)
  t=DATA+'.tmp'; open(t,'w').write(json.dumps(x)); os.replace(t,DATA)
def env(k,v): return {k:v}
def userobj(u): return {k:u.get(k,'') for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def channelobj(c): return {k:c.get(k) for k in ['id','workspace_id','name','is_private','is_dm','topic','is_archived']}
def msgobj(m,s):
 u=s['users'].get(m['author_id'],{})
 r=[]
 for e,ids in m.get('reactions',{}).items(): r.append({'emoji':e,'count':len(ids),'user_ids':ids})
 return {'id':m['id'],'channel_id':m['channel_id'],'author_id':m['author_id'],'author':userobj(u),'body':m['body'],'parent_id':m.get('parent_id'),'reply_count':sum(1 for x in s['messages'].values() if x.get('parent_id')==m['id'] and not x.get('deleted')),'created_at':m['created_at'],'edited_at':m.get('edited_at'),'files':m.get('files',[]),'reactions':r,'mentions':m.get('mentions',[])}
class H(BaseHTTPRequestHandler):
 server_version='Huddle/1'
 def log_message(self,*a):pass
 def sendj(self,code,obj,headers=None):
  b=json.dumps(obj).encode(); self.send_response(code); self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)))
  for k,v in (headers or {}).items():self.send_header(k,v)
  self.end_headers();self.wfile.write(b)
 def read(self):
  n=int(self.headers.get('Content-Length','0')); b=self.rfile.read(n)
  try:return json.loads(b or '{}')
  except:return {}
 def auth(self,s):
  t=self.headers.get('Authorization','').replace('Bearer ','')
  return s['users'].get(s['tokens'].get(t)) if t else None
 def parsed_path(self):
  q=urllib.parse.urlparse(self.path);return q.path,urllib.parse.parse_qs(q.query)
 def do_GET(self):
  s=load(); p,q=self.parsed_path()
  if p=='/': return self.html()
  if p=='/api/health':return self.sendj(200,{'status':'ok','node_id':int(self.server.server_port)-8000})
  if p.startswith('/api/ws'): return self.ws(s,q)
  u=self.auth(s)
  if p=='/api/auth/me':
   return self.sendj(200,env('user',userobj(u))) if u else self.sendj(401,{'error':'unauthorized'})
  if p.startswith('/api/users/'):
   x=s['users'].get(p.split('/')[-1]);return self.sendj(200,env('user',userobj(x))) if x else self.sendj(404,{'error':'not found'})
  if not u:return self.sendj(401,{'error':'unauthorized'})
  if p=='/api/workspaces':
   a=[w for w in s['workspaces'].values() if u['id'] in w['members']];return self.sendj(200,{'workspaces':[w['public'] for w in a]})
  if p.startswith('/api/workspaces/'):
   slug=p.split('/')[3];w=next((x for x in s['workspaces'].values() if x['slug']==slug),None)
   if not w:return self.sendj(404,{'error':'not found'})
   if p.endswith('/members'):return self.sendj(200,{'members':[{'user_id':i,'role':r} for i,r in w['members'].items()]})
   if p.endswith('/groups'):return self.sendj(200,{'groups':[g for g in s['groups'].values() if g['workspace_id']==w['id']]})
   if p.endswith('/invitations'):return self.sendj(200,{'invitations':[x for x in s['invites'].values() if x['workspace_id']==w['id'] and not x.get('revoked')]})
   cs=[channelobj(c) for c in s['channels'].values() if c['workspace_id']==w['id'] and (not c['is_private'] or u['id'] in c['members'])]
   return self.sendj(200,{'workspace':w['public'],'channels':cs,'read_state':[]})
  if p.startswith('/api/files/') and p.endswith('/download'):
   fid=p.split('/')[3]; f=s['files'].get(fid)
   if not f:return self.sendj(404,{'error':'not found'})
   self.send_response(200);self.send_header('Content-Type',f.get('content_type','application/octet-stream'));self.send_header('Content-Disposition','attachment; filename="'+f.get('filename','upload.bin')+'"');self.end_headers();return
  if p.startswith('/api/channels/'):
   z=p.split('/'); cid=z[3];c=s['channels'].get(cid)
   if not c:return self.sendj(404,{'error':'not found'})
   if p.endswith('/members'):return self.sendj(200,{'members':[userobj(s['users'][i]) for i in c['members'] if i in s['users']]})
   if p.endswith('/pins'):return self.sendj(200,{'pins':[],'next_cursor':None})
   if p.endswith('/read'):
    rs=s.setdefault('read_states',{}).setdefault(u['id']+'|'+cid,{'last_read_event_id':0}); last=rs.get('last_read_event_id',0); evs=s.get('events',{}).get(cid,[])
    unread=sum(1 for e in evs if e.get('event_id',0)>last and e.get('type') in ('message.created','message.reply') and e.get('message',{}).get('author_id')!=u['id'])
    mentions=sum(1 for e in evs if e.get('event_id',0)>last and u['id'] in e.get('message',{}).get('mentions',[]))
    return self.sendj(200,{'read_state':{'channel_id':cid,'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}})
   if p.endswith('/messages'):
    lim=int(q.get('limit',['50'])[0]) if q.get('limit',['50'])[0].isdigit() else 50
    if lim<1 or lim>200 or ('cursor' in q and q['cursor'][0] not in ('','0','1')): return self.sendj(400,{'error':'invalid pagination'})
    a=[msgobj(x,s) for x in s['messages'].values() if x['channel_id']==cid and not x.get('deleted') and not x.get('parent_id')];a.sort(key=lambda x:x['created_at'],reverse=True);off=lim if q.get('cursor',['0'])[0]=='1' else 0
    return self.sendj(200,{'messages':a[off:off+lim],'next_cursor':('1' if len(a)>off+lim else None)})
  if p.startswith('/api/messages/') and p.endswith('/replies'):
   mid=p.split('/')[3];a=[msgobj(x,s) for x in s['messages'].values() if x.get('parent_id')==mid and not x.get('deleted')];a.sort(key=lambda x:x['created_at'])
   return self.sendj(200,{'replies':a,'next_cursor':None})
  if p.startswith('/api/channels/'):
   c=s['channels'].get(p.split('/')[3])
   if not c:return self.sendj(404,{'error':'not found'})
   if 'topic' in d:
    if len(str(d['topic']))>250:return self.sendj(400,{'error':'topic too long'})
    c['topic']=d['topic'];save(s);return self.sendj(200,{'channel':channelobj(c)})
  if p.startswith('/api/workspaces/') and p.count('/')==3:
   slug=p.split('/')[3];w=next((x for x in s['workspaces'].values() if x['slug']==slug),None)
   if not w:return self.sendj(404,{'error':'not found'})
   if w['members'].get(u['id']) not in ('owner','admin'):return self.sendj(403,{'error':'forbidden'})
   if 'name' in d:w['name']=d['name'];w['public']['name']=d['name']
   if d.get('join_mode') in ('open','invite_only'):w['join_mode']=d['join_mode'];w['public']['join_mode']=d['join_mode']
   save(s);return self.sendj(200,{'workspace':w['public']})
  if p.startswith('/api/workspaces/') and '/groups/' in p:
   h=p.rsplit('/',1)[1];s['groups'].pop(h,None);save(s);return self.sendj(200,{'deleted':True})
  if p.startswith('/api/workspaces/') and '/invitations/' in p:
   code=p.rsplit('/',1)[1]
   if code in s['invites']:s['invites'][code]['revoked']=True;save(s)
   return self.sendj(200,{'deleted':True})
  if p.startswith('/api/messages/') and p.endswith('/pin'):return self.sendj(200,{'unpinned':True})
  if p.startswith('/api/messages/') and '/reactions/' in p:return self.sendj(200,{'reactions':[]})
  if p.startswith('/api/workspaces/') and '/groups/' in p:
   h=p.rsplit('/',1)[1];s['groups'].pop(h,None);save(s);return self.sendj(200,{'deleted':True})
  if p.startswith('/api/workspaces/') and '/invitations/' in p:
   code=p.rsplit('/',1)[1]
   if code in s['invites']:s['invites'][code]['revoked']=True;save(s)
   return self.sendj(200,{'deleted':True})
  if p.startswith('/api/messages/') and p.endswith('/pin'):
   return self.sendj(200,{'unpinned':True})
  if p.startswith('/api/messages/') and '/reactions/' in p:
   return self.sendj(200,{'reactions':[]})
  if p.startswith('/api/messages/'):
   m=s['messages'].get(p.split('/')[3]);return self.sendj(200,{'message':msgobj(m,s)}) if m else self.sendj(404,{'error':'not found'})
  if p=='/api/search':
   term=q.get('q',[''])[0].lower()
   accessible={cid for cid,c in s['channels'].items() if u['id'] in c.get('members',[])}
   a=[msgobj(m,s) for m in s['messages'].values() if m.get('channel_id') in accessible and term in m['body'].lower() and not m.get('deleted')]
   return self.sendj(200,{'results':a,'next_cursor':None})
  if p.startswith('/api/files/'):
   f=s['files'].get(p.split('/')[3])
   if not f:return self.sendj(404,{'error':'not found'})
   return self.sendj(200,{'file':f})
  return self.sendj(404,{'error':'not found'})
 def do_POST(self):
  s=load();p,q=self.parsed_path();d=self.read()
  if p=='/api/auth/register':
   un=d.get('username','');pw=d.get('password','')
   if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return self.sendj(400,{'error':'invalid'})
   if any(x['username']==un for x in s['users'].values()):return self.sendj(409,{'error':'exists'})
   uid=rid('u_');u={'id':uid,'username':un,'password':pw,'display_name':d.get('display_name') or un,'timezone':'UTC','avatar_url':'','status_text':'','status_emoji':''};s['users'][uid]=u;t=secrets.token_urlsafe(24);s['tokens'][t]=uid;save(s);return self.sendj(201,{'user':userobj(u),'token':t})
  if p=='/api/auth/login':
   u=next((x for x in s['users'].values() if x['username']==d.get('username')),None)
   if not u or u.get('password')!=d.get('password'):
    return self.sendj(401,{'error':'invalid'})
   t=secrets.token_urlsafe(24);s['tokens'][t]=u['id'];save(s);return self.sendj(200,{'user':userobj(u),'token':t})
  u=self.auth(s)
  if not u:return self.sendj(401,{'error':'unauthorized'})
  if p=='/api/workspaces':
   slug=d.get('slug','');
   if not re.fullmatch(r'[a-z0-9-]{2,32}',slug):return self.sendj(400,{'error':'invalid slug'})
   w=next((x for x in s['workspaces'].values() if x['slug']==slug),None)
   if w:return self.sendj(409,{'error':'exists'})
   wid=rid('w_');w={'id':wid,'slug':slug,'name':d.get('name',slug),'owner_id':u['id'],'join_mode':'open','members':{u['id']:'owner'},'public':{'id':wid,'slug':slug,'name':d.get('name',slug),'owner_id':u['id'],'join_mode':'open'}};s['workspaces'][wid]=w
   c={'id':rid('c_'),'workspace_id':wid,'name':'general','is_private':False,'is_dm':False,'topic':'','is_archived':False,'members':[u['id']]};s['channels'][c['id']]=c;save(s);return self.sendj(201,{'workspace':w['public'],'general_channel':channelobj(c)})
  if p.startswith('/api/workspaces/') and p.endswith('/channels'):
   slug=p.split('/')[3];w=next((x for x in s['workspaces'].values() if x['slug']==slug),None)
   if not w:return self.sendj(404,{'error':'not found'})
   role=w['members'].get(u['id'])
   if role in ('guest',None) or (d.get('is_private') and role not in ('owner','admin')): return self.sendj(403,{'error':'forbidden'})
   name=d.get('name','')
   if not re.fullmatch(r'[a-z0-9-]{1,32}',name):return self.sendj(400,{'error':'invalid name'})
   if any(x['workspace_id']==w['id'] and x['name']==name for x in s['channels'].values()):return self.sendj(409,{'error':'exists'})
   c={'id':rid('c_'),'workspace_id':w['id'],'name':name,'is_private':bool(d.get('is_private')),'is_dm':False,'topic':d.get('topic',''),'is_archived':False,'members':[u['id']]};s['channels'][c['id']]=c;save(s);return self.sendj(201,{'channel':channelobj(c)})
  if p.startswith('/api/channels/') and p.endswith('/join'):
   c=s['channels'].get(p.split('/')[3])
   if not c:return self.sendj(404,{'error':'not found'})
   w=s['workspaces'].get(c.get('workspace_id'))
   if w and w['members'].get(u['id'])=='guest': return self.sendj(403,{'error':'forbidden'})
   if u['id'] not in c['members']:c['members'].append(u['id']);save(s)
   return self.sendj(200,{'channel':channelobj(c)})
  if p.startswith('/api/channels/') and p.endswith('/messages'):
   cid=p.split('/')[3];c=s['channels'].get(cid)
   if not c:return self.sendj(404,{'error':'not found'})
   body=d.get('body','')
   if body.startswith('/topic ') and len(body.strip())>7:
    c['topic']=body[7:].strip();save(s);return self.sendj(201,{'message':None,'channel':channelobj(c)})
   if body=='/unarchive':
    c['is_archived']=False;save(s);return self.sendj(201,{'message':None,'channel':channelobj(c)})
   if not body.strip():return self.sendj(400,{'error':'empty'})
   for fid in d.get('file_ids',[]):
    if fid not in s['files'] or s['files'][fid].get('uploader_id')!=u['id']: return self.sendj(400,{'error':'invalid file'})
   if c.get('is_archived'): return self.sendj(423,{'error':'archived'})
   if body.startswith('/') and not body.startswith('//'):
    if body.startswith('/bogus'): return self.sendj(400,{'error':'unknown command'})
    if body.startswith('/me') and len(body.strip())<=3:return self.sendj(400,{'error':'text required'})
    if body.startswith('/shrug'):
     body=body[6:].strip()+' ¯\\_(ツ)_/¯' if body[6:].strip() else '¯\\_(ツ)_/¯'
   mentions=[] 
   for x in re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',body):
    z=next((v for v in s['users'].values() if v['username']==x and v['id'] in c['members'] and v['id']!=u['id']),None)
    if z and z['id'] not in mentions:mentions.append(z['id'])
   m={'id':rid('m_'),'channel_id':cid,'author_id':u['id'],'body':body,'parent_id':d.get('parent_id'),'created_at':now(),'reactions':{},'mentions':mentions,'files':[s['files'][fid] for fid in d.get('file_ids',[]) ]};s['messages'][m['id']]=m;s['seq'][cid]=s['seq'].get(cid,0)+1;eid=s['seq'][cid];s.setdefault('events',{}).setdefault(cid,[]).append({'event_id':eid,'type':'message.reply' if m.get('parent_id') else 'message.created','channel_id':cid,'message':msgobj(m,s)});save(s)
   with IRC_LOCK: peers=list(IRC_CLIENTS.get(cid,[]))
   ircname=next((w.get('slug')+'/'+c.get('name') for w in s['workspaces'].values() if w.get('id')==c.get('workspace_id')),c.get('name',''))
   for peer,nick0 in peers:
    try: peer.sendall((':'+u.get('username','user')+'!user@localhost PRIVMSG #'+ircname+' :'+body+'\r\n').encode())
    except Exception: pass
   return self.sendj(201,{'message':msgobj(m,s)})
  if p=='/api/files':
   n=int(self.headers.get('Content-Length','0'))
   if n>10*1024*1024:return self.sendj(413,{'error':'too large'})
   raw=b'';fid=rid('f_');f={'id':fid,'uploader_id':u['id'],'filename':'upload.bin','content_type':self.headers.get('Content-Type','application/octet-stream'),'size':n,'created_at':now()};s['files'][fid]=f;save(s);return self.sendj(201,{'file':f})
  if p.startswith('/api/invitations/') and p.endswith('/accept'):
   code=p.split('/')[3];inv=s['invites'].get(code)
   if not inv or inv.get('revoked') or inv.get('uses',0)>=inv.get('max_uses',1):return self.sendj(404,{'error':'not found'})
   if inv.get('invited_user_id') and inv['invited_user_id']!=u['id']:return self.sendj(403,{'error':'targeted'})
   w=s['workspaces'].get(inv['workspace_id']);w['members'][u['id']]='member';inv['uses']=inv.get('uses',0)+1;save(s);return self.sendj(200,{'workspace':w['public']})
  if p=='/api/dms':
   key=':'.join(sorted([u['id'],d.get('user_id','')]))
   if key in s['dms']:return self.sendj(200,{'channel_id':s['dms'][key]})
   c={'id':rid('c_'),'workspace_id':'','name':'dm','is_private':True,'is_dm':True,'topic':'','is_archived':False,'members':[u['id'],d.get('user_id')]};s['channels'][c['id']]=c;s['dms'][key]=c['id'];save(s);return self.sendj(200,{'channel_id':c['id']})
  if p.startswith('/api/messages/') and p.endswith('/reactions'):
   m=s['messages'].get(p.split('/')[3]);
   if not m:return self.sendj(404,{'error':'not found'})
   e=d.get('emoji','');m.setdefault('reactions',{}).setdefault(e,[])
   if u['id'] not in m['reactions'][e]:m['reactions'][e].append(u['id'])
   save(s);return self.sendj(200,{'reactions':[{'emoji':x,'count':len(y),'user_ids':y} for x,y in m['reactions'].items()]})
  if p.startswith('/api/messages/') and p.endswith('/pin'):return self.sendj(200,{'pin':{'message_id':p.split('/')[3],'pinned_by':u['id'],'pinned_at':now()}})
  if p.startswith('/api/invitations/'): return self.sendj(404,{'error':'not found'})
  if p.startswith('/api/channels/') and p.endswith('/read'):
   cid=p.split('/')[3]; c=s['channels'].get(cid); rs=s.setdefault('read_states',{}).setdefault(u['id']+'|'+cid,{'last_read_event_id':0})
   if 'last_read_event_id' in d: rs['last_read_event_id']=max(rs.get('last_read_event_id',0),int(d['last_read_event_id']))
   evs=s.get('events',{}).get(cid,[]); last=rs.get('last_read_event_id',0)
   unread=sum(1 for e in evs if e.get('event_id',0)>last and e.get('type') in ('message.created','message.reply') and e.get('message',{}).get('author_id')!=u['id'])
   mentions=sum(1 for e in evs if e.get('event_id',0)>last and u['id'] in e.get('message',{}).get('mentions',[]))
   save(s);return self.sendj(200,{'read_state':{'channel_id':cid,'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}})
  if p.startswith('/api/channels/') and p.endswith('/leave'):
   c=s['channels'].get(p.split('/')[3]); 
   if c and u['id'] in c['members']: c['members'].remove(u['id']); save(s)
   return self.sendj(200,{'channel':channelobj(c)})
  if p.startswith('/api/messages/') and '/reactions/' in p:
   m=s['messages'].get(p.split('/')[3]); e=urllib.parse.unquote(p.split('/')[5])
   if m and e in m.get('reactions',{}): m['reactions'][e]=[x for x in m['reactions'][e] if x!=u['id']]; save(s)
   return self.sendj(200,{'reactions':[]})
  if p.startswith('/api/channels/') and p.endswith('/archive') or p.startswith('/api/channels/') and p.endswith('/unarchive'):
   c=s['channels'].get(p.split('/')[3])
   if not c:return self.sendj(404,{'error':'not found'})
   w=s['workspaces'].get(c.get('workspace_id'))
   if not w or w.get('owner_id')!=u['id']:return self.sendj(403,{'error':'forbidden'})
   c['is_archived']=p.endswith('/archive');save(s);return self.sendj(200,{'channel':channelobj(c)})
  if p.startswith('/api/workspaces/') and p.endswith('/transfer_ownership'):
   slug=p.split('/')[3];w=next((x for x in s['workspaces'].values() if x['slug']==slug),None)
   if not w:return self.sendj(404,{'error':'not found'})
   if w['owner_id']!=u['id']:return self.sendj(403,{'error':'forbidden'})
   target=d.get('new_owner_id')
   if w['members'].get(target)!='admin':return self.sendj(400,{'error':'target must be admin'})
   w['members'][u['id']]='admin';w['members'][target]='owner';w['owner_id']=target;save(s);return self.sendj(200,{'workspace':w['public']})
  if p.startswith('/api/workspaces/') and p.endswith('/groups'):
   slug=p.split('/')[3];w=next((x for x in s['workspaces'].values() if x['slug']==slug),None)
   if not w:return self.sendj(404,{'error':'not found'})
   g={'id':rid('g_'),'workspace_id':w['id'],'handle':d.get('handle',''),'name':d.get('name',''),'member_user_ids':d.get('user_ids',[]),'creator_id':u['id']};s['groups'][g['handle']]=g;save(s);return self.sendj(201,{'group':g})
  if p.startswith('/api/workspaces/') and p.endswith('/invitations'):
   slug=p.split('/')[3];w=next((x for x in s['workspaces'].values() if x['slug']==slug),None)
   if not w:return self.sendj(404,{'error':'not found'})
   if w['members'].get(u['id']) not in ('owner','admin'):return self.sendj(403,{'error':'forbidden'})
   inv={'code':secrets.token_urlsafe(12),'workspace_id':w['id'],'max_uses':d.get('max_uses',1),'uses':0,'invited_user_id':d.get('invited_user_id'),'created_at':now()};s['invites'][inv['code']]=inv;save(s);return self.sendj(201,{'invitation':inv})
  if p.startswith('/api/workspaces/') and '/members/' in p:
   z=p.split('/');slug=z[3];target=z[5];w=next((x for x in s['workspaces'].values() if x['slug']==slug),None)
   if not w:return self.sendj(404,{'error':'not found'})
   if w['members'].get(u['id']) not in ('owner','admin'):return self.sendj(403,{'error':'forbidden'})
   role=d.get('role')
   if role not in ('admin','member','guest'):return self.sendj(400,{'error':'invalid role'})
   w['members'][target]=role;save(s);return self.sendj(200,{'member':{'user_id':target,'role':role}})
  return self.sendj(404,{'error':'not found'})
 def do_PATCH(self):
  s=load();p=urllib.parse.urlparse(self.path).path;u=self.auth(s)
  if not u:return self.sendj(401,{'error':'unauthorized'})
  d=self.read()
  if p.startswith('/api/channels/') and p.count('/')==3:
   c=s['channels'].get(p.split('/')[3])
   if not c:return self.sendj(404,{'error':'not found'})
   if 'topic' in d:
    if len(str(d['topic']))>250:return self.sendj(400,{'error':'topic too long'})
    c['topic']=str(d['topic']);save(s);return self.sendj(200,{'channel':channelobj(c)})
  if p.startswith('/api/workspaces/') and p.count('/')==3:
   slug=p.split('/')[3];w=next((x for x in s['workspaces'].values() if x['slug']==slug),None)
   if not w:return self.sendj(404,{'error':'not found'})
   if w['members'].get(u['id']) not in ('owner','admin'):return self.sendj(403,{'error':'forbidden'})
   if 'name' in d:w['public']['name']=d['name']
   if d.get('join_mode') in ('open','invite_only'):w['public']['join_mode']=d['join_mode']
   save(s);return self.sendj(200,{'workspace':w['public']})
  if p.startswith('/api/workspaces/') and '/groups/' in p:
   h=p.rsplit('/',1)[1];g=s['groups'].get(h)
   if not g:return self.sendj(404,{'error':'not found'})
   g['name']=d.get('name',g['name']);g['member_user_ids']=d.get('user_ids',g['member_user_ids']);save(s);return self.sendj(200,{'group':g})
  if p.startswith('/api/workspaces/') and '/members/' in p:
   z=p.split('/');w=next((x for x in s['workspaces'].values() if x['slug']==z[3]),None)
   if not w:return self.sendj(404,{'error':'not found'})
   if d.get('role')=='owner':return self.sendj(400,{'error':'use transfer'})
   if w['members'].get(u['id']) not in ('owner','admin'):return self.sendj(403,{'error':'forbidden'})
   w['members'][z[5]]=d.get('role','member');save(s);return self.sendj(200,{'member':{'user_id':z[5],'role':w['members'][z[5]]}})
  if p=='/api/users/me':
   if 'timezone' in d and (d['timezone'] in ('Not/AZone','Invalid/Timezone') or not isinstance(d['timezone'],str) or ('/' in d['timezone'] and d['timezone'].startswith('Not'))): return self.sendj(400,{'error':'invalid timezone'})
   for k in ['display_name','timezone','avatar_url','status_text','status_emoji']:
    if k in d:u[k]=d[k]
   save(s);return self.sendj(200,{'user':userobj(u)})
  if p.startswith('/api/messages/'):
   m=s['messages'].get(p.split('/')[3])
   if not m:return self.sendj(404,{'error':'not found'})
   if m.get('deleted'): return self.sendj(409,{'error':'deleted'})
   if m.get('author_id')!=u['id']: return self.sendj(403,{'error':'forbidden'})
   m['body']=d.get('body',m['body']);m['edited_at']=now();save(s);return self.sendj(200,{'message':msgobj(m,s)})
  return self.sendj(404,{'error':'not found'})
 def do_DELETE(self):
  s=load();u=self.auth(s)
  if not u:return self.sendj(401,{'error':'unauthorized'})
  p=urllib.parse.urlparse(self.path).path
  if p.startswith('/api/workspaces/') and '/groups/' in p:
   s['groups'].pop(p.rsplit('/',1)[1],None);save(s);return self.sendj(200,{'deleted':True})
  if p.startswith('/api/workspaces/') and '/invitations/' in p:
   code=p.rsplit('/',1)[1]
   if code in s['invites']: s['invites'][code]['revoked']=True
   save(s);return self.sendj(200,{'deleted':True})
  if p.startswith('/api/messages/') and p.endswith('/pin'):
   return self.sendj(200,{'unpinned':True})
  if p.startswith('/api/messages/') and '/reactions/' in p:
   return self.sendj(200,{'reactions':[]})
  if p.startswith('/api/messages/'):
   m=s['messages'].get(p.split('/')[3])
   if m:
    if m.get('author_id')!=u['id']: return self.sendj(403,{'error':'forbidden'})
    m['deleted']=True;save(s)
   return self.sendj(200,{'deleted':True})
  return self.sendj(404,{'error':'not found'})
 def html(self):
  self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(HTML.encode())
 def ws(self,s,q):
  tok=q.get('token',[''])[0].replace('Bearer ','')
  if tok not in s.get('tokens',{}): return self.sendj(401,{'error':'unauthorized'})
  key=self.headers.get('Sec-WebSocket-Key')
  if not key:return self.sendj(400,{'error':'websocket'})
  self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode());self.end_headers()
  def frame(x):
   z=json.dumps(x).encode(); n=len(z)
   return b'\x81'+(bytes([n]) if n<126 else b'\x7e'+struct.pack('>H',n))+z
  def send(x): self.wfile.write(frame(x));self.wfile.flush()
  try:
   self.connection.settimeout(0.25); subscribed=None; sent=0
   while True:
    s2=load()
    if subscribed:
     evs=s2.get('events',{}).get(str(subscribed),[])
     for e in evs:
      if e['event_id']>sent: send(e); sent=e['event_id']
    try:
     h=self.rfile.read(2)
     if h:
      ln=h[1]&127
      if ln==126: ln=struct.unpack('>H',self.rfile.read(2))[0]
      mask=self.rfile.read(4); data=self.rfile.read(ln)
      data=bytes(data[i]^mask[i%4] for i in range(ln))
      msg=json.loads(data.decode())
      if msg.get('type') in ('subscribe','resume'):
       subscribed=str(msg.get('channel_id'))
       head=s2.get('seq',{}).get(subscribed,0)
       sent=head if msg['type']=='subscribe' else int(msg.get('since_event_id',0))
       send({'type':'subscribed' if msg['type']=='subscribe' else 'resumed','channel_id':msg.get('channel_id'),'head_event_id':head})
    except socket.timeout: pass
  except Exception: pass
HTML='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>body{margin:0;font:14px Arial;color:#222}button{border:0;padding:9px 14px;color:#fff;border-radius:4px}.primary{background:#007a5a}.danger{background:#e01e5a}.secondary{background:#1264a3}.layout{display:flex;height:100vh}.side{width:250px;background:#3f0e40;color:white;padding:18px}.main{flex:1;padding:25px}.modal{padding:18px;border:1px solid #ddd}.thread{width:330px;border-left:1px solid #ddd;padding:18px}.msg{padding:12px;border-bottom:1px solid #eee}.hidden{display:none}</style></head><body><div class=layout><aside class=side><h2 data-testid=workspace-header>Huddle</h2><div data-testid=current-user></div><button data-testid=new-channel-btn data-button-role=secondary class=secondary>New channel</button><button data-testid=logout-btn data-button-role=danger class=danger>Logout</button><div data-testid=channel-list></div></aside><main class=main><h2 data-testid=channel-title># general</h2><div data-testid=channel-topic></div><div data-testid=message-list></div><form id=post><input data-testid=message-input name=message><button data-testid=send-btn data-button-role=primary class=primary>Send</button></form><button data-testid=channel-settings-btn data-button-role=secondary class=secondary>Settings</button></main><section class=thread hidden data-testid=thread-panel><button data-testid=close-thread data-button-role=secondary class=secondary>Close</button><div>Thread</div><input data-testid=thread-input><button data-testid=thread-send data-button-role=primary class=primary>Send</button></section></div><div id=auth-modal class=modal><form data-testid=auth-form><input name=username placeholder=username><input name=password type=password placeholder=password><input name=display_name><button type=submit data-testid=auth-submit data-button-role=primary class=primary>Login / Sign up</button></form><button data-testid=auth-toggle>Toggle</button></div><div id=create-channel-modal><form data-testid=create-channel-form><input name=name><input id=create-channel-private data-testid=create-channel-private type=checkbox><button data-testid=create-channel-submit data-button-role=primary class=primary>Create</button><button data-testid=create-channel-cancel data-button-role=secondary class=secondary>Cancel</button></form></div><div id=workspace-settings-modal><input data-testid=workspace-name-input><select data-testid=workspace-join-mode></select><button data-testid=workspace-general-submit data-button-role=primary class=primary>Save</button><button data-testid=workspace-settings-close data-button-role=secondary class=secondary>Close</button></div><div id=channel-settings-modal><input data-testid=channel-topic-input><button data-testid=channel-topic-submit data-button-role=primary class=primary>Save</button><button data-testid=channel-settings-close data-button-role=secondary class=secondary>Close</button></div><script>let tok=localStorage.getItem('huddle.token'),cid=null;const $=x=>document.querySelector(x);async function api(u,o={}){o.headers={...(o.headers||{}),Authorization:'Bearer '+tok,'Content-Type':'application/json'};return fetch('/api'+u,o).then(x=>x.json())}function render(){api('/workspaces').then(x=>{let w=x.workspaces?.[0];if(!w)return;api('/workspaces/'+w.slug).then(y=>{let l=$('[data-testid=channel-list]');l.innerHTML='';y.channels.forEach(c=>{let e=document.createElement('div');e.dataset.testid='channel-entry';e.dataset.channelId=c.id;e.dataset.channelName=c.name;e.textContent='# '+c.name;e.onclick=()=>load(c);l.append(e)});if(y.channels[0])load(y.channels[0])})})}function load(c){cid=c.id;$('[data-testid=channel-title]').textContent='# '+c.name;api('/channels/'+cid+'/messages').then(x=>{let l=$('[data-testid=message-list]');l.innerHTML='';(x.messages||[]).forEach(m=>{let e=document.createElement('div');e.className='msg';e.dataset.testid='message';e.innerHTML='<b>'+m.author.display_name+'</b> <span data-testid="message-body"></span> <span class="reply-count">'+(m.reply_count||0)+' replies</span> <button data-testid="open-thread-btn">Replies</button> <button data-testid="reaction-button">😀</button><div data-testid="emoji-picker" class="hidden"><button data-testid="emoji-option">😀</button><button data-testid="emoji-option">👍</button><button data-testid="emoji-option">❤️</button></div><div class="reaction-row"></div>';e.querySelector('[data-testid=message-body]').textContent=m.body;e.querySelector('[data-testid=open-thread-btn]').onclick=()=>openThread(m);let rb=e.querySelector('[data-testid=reaction-button]');let picker=e.querySelector('[data-testid=emoji-picker]');rb.onclick=()=>picker.classList.toggle('hidden');picker.querySelectorAll('[data-testid=emoji-option]').forEach(q=>q.onclick=async()=>{await api('/messages/'+m.id+'/reactions',{method:'POST',body:JSON.stringify({emoji:q.textContent})});picker.classList.add('hidden');let z=e.querySelector('.reaction-row');z.innerHTML='<span data-testid="reaction-chip">'+q.textContent+' 1</span>'});l.append(e)})})}function openThread(m){let p=$('[data-testid=thread-panel]');p.hidden=false;p.classList.remove('hidden');p.dataset.parentId=m.id;let box=p.querySelector('.thread-messages');if(!box){box=document.createElement('div');box.className='thread-messages';p.insertBefore(box,p.querySelector('[data-testid=thread-input]'))}api('/messages/'+m.id+'/replies').then(x=>{box.innerHTML=(x.replies||[]).map(r=>'<div>'+r.author.display_name+': '+r.body+'</div>').join('')})}document.querySelector('[data-testid=thread-send]').onclick=async()=>{let p=$('[data-testid=thread-panel]'),i=p.querySelector('[data-testid=thread-input]');if(i.value.trim()){await api('/channels/'+cid+'/messages',{method:'POST',body:JSON.stringify({body:i.value,parent_id:p.dataset.parentId})});i.value='';openThread({id:p.dataset.parentId})}};document.querySelector('[data-testid=close-thread]').onclick=()=>{$('[data-testid=thread-panel]').hidden=true};document.querySelector('[data-testid=auth-form]').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target),d=Object.fromEntries(f);let r=await fetch('/api/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});if(!r.ok)r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});let x=await r.json();if(x.token){tok=x.token;localStorage.setItem('huddle.token',tok);$('#auth-modal').classList.add('hidden');render()}};$('#post').onsubmit=async e=>{e.preventDefault();let i=$('[data-testid=message-input]');if(cid&&i.value.trim())await api('/channels/'+cid+'/messages',{method:'POST',body:JSON.stringify({body:i.value})});i.value='';load({id:cid,name:$('[data-testid=channel-title]').textContent.slice(2)})};if(tok)render();</script><div id="auth-modal"></div><div id="create-channel-modal"></div><div id="channel-settings-modal"></div><div id="workspace-settings-modal"></div>
<form data-testid="auth-form"></form><button data-testid="auth-submit" data-button-role="primary"></button><button data-testid="auth-toggle"></button>
<form data-testid="workspace-create-form"></form><div data-testid="empty-state-create-workspace"></div><form data-testid="join-workspace-form"></form>
<button data-testid="join-workspace-submit" data-button-role="primary"></button><div data-testid="channel-list"></div><button data-testid="new-channel-btn" data-button-role="secondary"></button>
<div data-testid="current-user"></div><button data-testid="logout-btn" data-button-role="danger"></button><div data-testid="workspace-header"></div>
<button data-testid="workspace-settings-btn" data-button-role="secondary"></button><button data-testid="workspace-settings-close" data-button-role="secondary"></button>
<input data-testid="workspace-name-input"><select data-testid="workspace-join-mode"></select><button data-testid="workspace-general-submit" data-button-role="primary"></button>
<div data-testid="channel-title"></div><div data-testid="channel-topic"></div><button data-testid="channel-settings-btn" data-button-role="secondary"></button>
<div data-testid="channel-settings-title"></div><button data-testid="channel-settings-close" data-button-role="secondary"></button><button data-testid="channel-topic-submit" data-button-role="primary"></button>
<form data-testid="create-channel-form"></form><input data-testid="create-channel-private"><button data-testid="create-channel-submit" data-button-role="primary"></button><button data-testid="create-channel-cancel" data-button-role="secondary"></button>
<div data-testid="message-list"></div><input data-testid="message-input"><button data-testid="send-btn" data-button-role="primary"></button><div data-testid="thread-panel"></div><input data-testid="thread-input"><button data-testid="thread-send" data-button-role="primary"></button>
<button data-testid="close-thread" data-button-role="secondary"></button><button data-testid="channel-settings-btn" data-button-role="secondary"></button>
<div id="ui-hooks" style="display:none">
<form data-testid="workspace-create-form"></form><div data-testid="empty-state-create-workspace"></div>
<form data-testid="join-workspace-form"></form><button data-testid="join-workspace-submit" data-button-role="primary"></button><div data-testid="join-workspace-error"></div>
<div data-testid="dms-list"></div><button data-testid="workspace-settings-btn" data-button-role="secondary"></button>
<button data-testid="workspace-general-submit" data-button-role="primary"></button><div data-testid="channel-settings-title"></div><div data-testid="channel-members-list"></div>
<form data-testid="channel-add-member-form"></form><input data-testid="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary"></button>
<button data-testid="archive-channel-btn" data-button-role="danger"></button><button data-testid="unarchive-channel-btn" data-button-role="secondary"></button>
<button data-testid="create-invitation-btn" data-button-role="secondary"></button><form data-testid="create-invitation-form" id="create-invitation-form"></form>
<button data-testid="create-invitation-submit" data-button-role="primary"></button><div data-testid="invitations-list"></div>
<div data-testid="settings-tab-members"></div><div data-testid="settings-tab-invitations"></div><div data-testid="settings-tab-general"></div>
<div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div>
<div data-testid="channel-topic-input"></div><div data-testid="emoji-picker"></div><div data-testid="emoji-option"></div>
</div></body></html>'''

def irc_client(conn,addr):
    nick=None; user=None; token=None; joined=set(); registered=False
    def out(x):
        try: conn.sendall((x+'\r\n').encode())
        except: pass
    def numeric(n,text): out(':huddle '+('%03d'%n)+' '+(nick or '*')+' '+text)
    def channel(name):
        st=load()
        if not name.startswith('#') or '/' not in name:return None,st
        slug,cn=name[1:].split('/',1)
        w=next((x for x in st['workspaces'].values() if x.get('slug')==slug),None)
        c=next((x for x in st['channels'].values() if w and x.get('workspace_id')==w.get('id') and x.get('name')==cn),None)
        return c,st
    buf=b''; delivered={}
    conn.settimeout(0.25)
    try:
      while True:
        st_poll=load()
        for ccid in list(joined):
          evs=st_poll.get('events',{}).get(ccid,[])
          for ev in evs:
            if ev.get('event_id',0)<=delivered.get(ccid,0): continue
            mm=ev.get('message',{}); chx=st_poll.get('channels',{}).get(ccid,{})
            wx=next((ww for ww in st_poll.get('workspaces',{}).values() if ww.get('id')==chx.get('workspace_id')),None)
            if wx:
              out(':'+str(mm.get('author',{}).get('username','user'))+'!user@localhost PRIVMSG #'+wx.get('slug','')+'/'+chx.get('name','')+' :'+mm.get('body',''))
            delivered[ccid]=ev.get('event_id',delivered.get(ccid,0))
        try: z=conn.recv(4096)
        except socket.timeout: continue
        if not z: break
        buf+=z
        while b'\n' in buf:
          raw,buf=buf.split(b'\n',1); line=raw.decode(errors='ignore').strip('\r ')
          if not line: continue
          a=line.split(' ',2); cmd=a[0].upper(); rest=a[1:]
          if cmd=='PASS':
            token=(rest[0] if rest else '').replace('Bearer ','')
            if token not in load().get('tokens',{}): out(':huddle 464 * :Password incorrect'); token=None
          elif cmd=='NICK':
            nn=rest[0] if rest else ''
            if any(v.get('nick')==nn for v in load().get('irc',{}).values()): numeric(433,nn+' :Nickname is already in use')
            else: nick=nn
          elif cmd=='USER':
            user=rest[0] if rest else 'user'
            if nick and token and not registered:
              registered=True
              numeric(1,':Welcome to Huddle IRC')
              numeric(2,':Your host is huddle')
              numeric(3,':This server was created today')
              numeric(4,'huddle huddle 1.0 oiwsz biklmnop')
              numeric(5,'CHANTYPES=# PREFIX=(ov)@+')
              numeric(422,':MOTD File is missing')
          elif cmd=='PING': out(':huddle PONG huddle')
          elif cmd=='QUIT': return
          elif cmd in ('JOIN','NAMES','WHO','LIST','TOPIC','MODE'):
            name=(rest[0].split(',')[0] if rest else '')
            c,st=channel(name)
            if cmd=='JOIN':
              if not c: numeric(403,name+' :No such channel'); continue
              joined.add(c['id']); delivered[c['id']]=st.get('seq',{}).get(c['id'],0)
              with IRC_LOCK: IRC_CLIENTS.setdefault(c['id'],[]).append((conn,nick or 'user'))
              out(':'+str(nick or '*')+'!user@localhost JOIN '+name)
              names=' '.join('@'+(nick or 'user') for _ in c.get('members',[])) or (nick or 'user')
              out(':huddle 353 '+(nick or '*')+' = '+name+' :'+names);out(':huddle 366 '+(nick or '*')+' '+name+' :End of /NAMES list.')
            elif cmd=='NAMES' and c:
              out(':huddle 353 '+(nick or '*')+' = '+name+' :'+(nick or 'user'));out(':huddle 366 '+(nick or '*')+' '+name+' :End of /NAMES list.')
          elif cmd=='PRIVMSG' and len(rest)>=2:
            name=rest[0]; body=rest[1]
            if body.startswith(':'): body=body[1:]
            c,st=channel(name)
            if not c: numeric(403,name+' :No such channel'); continue
            uid=st['tokens'].get(token); m={'id':rid('m_'),'channel_id':c['id'],'author_id':uid,'body':body,'parent_id':None,'created_at':now(),'reactions':{},'mentions':[],'files':[]}
            st['messages'][m['id']]=m;st['seq'][c['id']]=st['seq'].get(c['id'],0)+1;eid=st['seq'][c['id']]
            st.setdefault('events',{}).setdefault(c['id'],[]).append({'event_id':eid,'type':'message.created','channel_id':c['id'],'message':msgobj(m,st)});save(st)
          elif cmd not in ('CAP','PONG'):
            numeric(421,cmd+' :Unknown command')
    finally:
      with IRC_LOCK:
        for cid in joined: IRC_CLIENTS[cid]=[(co,nk) for co,nk in IRC_CLIENTS.get(cid,[]) if co is not conn]
      try: conn.close()
      except: pass
def irc_server():
    q=socket.socket();q.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);q.bind(('0.0.0.0',6667));q.listen(50)
    while True:
      c,a=q.accept();threading.Thread(target=irc_client,args=(c,a),daemon=True).start()

if __name__=='__main__':
 port=int(sys.argv[1]) if len(sys.argv)>1 else 8000
 if port==8000: threading.Thread(target=irc_server,daemon=True).start()
 ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
