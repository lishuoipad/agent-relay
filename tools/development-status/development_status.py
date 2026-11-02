"""Read-only local development dashboard; no messages, network or task control."""
from __future__ import annotations
import argparse, collections, datetime as dt, json, os, pathlib, re, sqlite3, sys, time
DB = CHECKPOINTS = PLAN = None
TZ = dt.timezone.utc
KNOWN = []
FRIENDLY_PLAN = {}
RULES = []
REPLACEMENTS = {}
PLAN_ID_PATTERN = r"\b[A-Z][A-Z0-9]*-\d+(?:[A-Za-z0-9.-]*)\b"


def configure(config_path):
 """Read one explicitly selected local profile; relative paths belong to it."""
 global DB, CHECKPOINTS, PLAN, TZ, KNOWN, FRIENDLY_PLAN, RULES, REPLACEMENTS, PLAN_ID_PATTERN
 path = pathlib.Path(config_path).expanduser().resolve()
 settings = json.loads(path.read_text(encoding='utf-8-sig'))
 def local_path(key):
  value = settings[key]
  if not isinstance(value, str) or not value: raise ValueError('Missing profile path: ' + key)
  result = pathlib.Path(os.path.expandvars(value)).expanduser()
  return result.resolve() if result.is_absolute() else (path.parent / result).resolve()
 DB, CHECKPOINTS, PLAN = (local_path(k) for k in ('state_db', 'checkpoints', 'plan'))
 TZ = dt.timezone(dt.timedelta(hours=settings.get('timezone_offset_hours', 0)))
 threads = settings['threads']
 if not isinstance(threads, list): raise ValueError('threads must be a list')
 KNOWN = [(t['id'], t['name'], t.get('scope', '')) for t in threads]
 if any(not isinstance(t[0], str) or not t[0] for t in KNOWN): raise ValueError('thread id is required')
 if len(KNOWN) > 120: raise ValueError('at most 120 root threads')
 if len({t[0] for t in KNOWN}) != len(KNOWN): raise ValueError('duplicate thread id')
 FRIENDLY_PLAN = settings.get('friendly_plan', {})
 REPLACEMENTS = settings.get('text_replacements', {})
 PLAN_ID_PATTERN = settings.get('plan_id_pattern', r"\b[A-Z][A-Z0-9]*-\d+(?:[A-Za-z0-9.-]*)\b")
 re.compile(PLAN_ID_PATTERN)
 RULES = []
 for rule in settings.get('rules', []):
  if not isinstance(rule['summary'], str) or not isinstance(rule['validation'], str): raise ValueError('rule text must be strings')
  if not isinstance(rule['plan_ids'], list) or any(not isinstance(x, str) for x in rule['plan_ids']): raise ValueError('plan_ids must be strings')
  RULES.append((re.compile(rule['pattern']), rule['summary'], rule['plan_ids'], rule['validation']))
 return settings

def parse_at(value):
 try:
  return dt.datetime.fromisoformat(str(value).replace('Z','+00:00')).astimezone(TZ)
 except (ValueError,TypeError):return None

def plain(text):
 text=re.sub(r'\[([^\]]+)\]\([^)]*\)',r'\1',str(text))
 replacements=REPLACEMENTS
 for old,new in replacements.items():text=text.replace(old,new)
 text=re.sub(r'`[^`]*`','',text)
 text=re.sub(r'\b[0-9a-f]{8,64}\b','',text,flags=re.I)
 text=re.sub(r'[A-Z]:[/\\][^\s，。；\n]+','（文件位置见详情）',text)
 text=re.sub(r'\s+',' ',text).strip()
 return text[:180]+('…' if len(text)>180 else '')

def describe(text):
 """Ordered project rules are associations, never assignment or acceptance."""
 for pattern, summary, ids, validation in RULES:
  if pattern.search(text): return summary, list(ids), validation
 explicit = re.findall(PLAN_ID_PATTERN, text)
 readable = plain(text)
 if not readable: readable = '记录中暂时没有可读的工作说明'
 return readable, list(dict.fromkeys(explicit))[:2], '尚无可归纳的验证结论'

def status_from(checkpoint, latest_event, public, now):
 task=str(checkpoint.get('task',''))
 if re.search(r'原Goal仍paused|Goal\s*(?:仍)?paused|已暂停|暂停开发',task,re.I):
  return '已暂停开发','paused'
 cp_at=parse_at(checkpoint.get('updated_at'))
 event_at=parse_at(latest_event.get('at')) if latest_event else None
 if latest_event and latest_event['kind']=='task_started' and (not cp_at or (event_at and event_at>cp_at)):
  return '本轮处理中（有开始记录）','working'
 if re.search(r'待接收|等待.*接收|已交Root|已交付|交Root待',task):
  return '已交付，等待主线接收','delivered'
 if latest_event:
  if latest_event['kind']=='task_complete':
   if latest_event.get('error'):return '本轮已结束，存在异常','ended'
   return '本轮已结束','ended'
  if latest_event['kind']=='turn_aborted':return '本轮已中断','paused'
 if public and public.get('phase')=='final_answer':return '已回复结果','ended'
 if public and re.search(r'会一起修|正在.*修|补取资料',public.get('text','')):return '修正中（最近说明）','working'
 if public and re.search(r'先核对|先检查|只提出.*候选',public.get('text','')):return '检查中（最近说明）','working'
 return '状态未确认（仅有进展记录）','unknown'

class Tail:
 def __init__(self,path,limit):
  self.path=pathlib.Path(path);self.limit=limit;self.offset=None;self.pending=b''
  self.messages=collections.deque(maxlen=6);self.event={};self.read_at=None
 def read(self):
  size=self.path.stat().st_size
  if self.offset==size:return
  dropped=False
  if self.offset is None or self.offset>size or size-self.offset>self.limit:
   start=max(0,size-self.limit);self.pending=b'';dropped=start>0
  else:start=self.offset
  with self.path.open('rb') as f:f.seek(start);data=f.read(self.limit)
  self.offset=start+len(data)
  rows=(self.pending+data).split(b'\n');self.pending=rows.pop()
  if len(self.pending)>self.limit:self.pending=b''
  if dropped and rows:rows=rows[1:]
  for raw in rows:
   # Do not parse or retain reasoning/tool payloads.
   is_public=b'"response_item"' in raw and b'"message"' in raw and (b'"commentary"' in raw or b'"final_answer"' in raw)
   is_event=b'"event_msg"' in raw and any(x in raw for x in (b'"task_started"',b'"task_complete"',b'"turn_aborted"'))
   if not (is_public or is_event):continue
   try:e=json.loads(raw);p=e.get('payload',{})
   except (ValueError,TypeError):continue
   if is_public and e.get('type')=='response_item' and p.get('type')=='message' and p.get('role')=='assistant' and p.get('phase') in ('commentary','final_answer'):
    body='\n'.join(v.get('text','') for v in p.get('content',[]) if v.get('type') in ('text','output_text')).strip()
    if body:
     m={'at':e.get('timestamp'),'phase':p['phase'],'text':body[:5000]}
     if not self.messages or self.messages[-1]!=m:self.messages.append(m)
   if is_event and e.get('type')=='event_msg' and p.get('type') in ('task_started','task_complete','turn_aborted'):
    self.event={'at':e.get('timestamp'),'kind':p['type'],'error':bool(p.get('error'))}

class Collector:
 def __init__(self, config_path):
  configure(config_path)
  self.catalog={};self.tails={};self.catalog_at=0;self.catalog_mode='';self.warnings=[]
  self.plan={};self.plan_sig=None
 def plans(self):
  sig=(PLAN.stat().st_mtime_ns,PLAN.stat().st_size)
  if sig!=self.plan_sig:
   obj=json.loads(PLAN.read_text(encoding='utf-8-sig'));index={}
   def add(t):
    index[t['id']]={k:t.get(k) for k in ['id','title','status','acceptance']}
    for sub in t.get('subtasks',[]):add(sub)
   for t in obj['tasks']:add(t)
   self.plan=index;self.plan_sig=sig
 def catalog_refresh(self):
  if time.monotonic()-self.catalog_at<30:return
  try:
   conn=sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True,timeout=1)
   conn.execute('PRAGMA query_only=ON');self.catalog_mode='只读实时登记'
  except sqlite3.Error:
   conn=sqlite3.connect(DB.as_uri()+'?mode=ro&immutable=1',uri=True,timeout=1)
   self.catalog_mode='只读磁盘快照（发现新智能体可能延迟）'
  try:
   conn.row_factory=sqlite3.Row
   keys='id,rollout_path,source,agent_path,agent_nickname,updated_at,archived'
   new={};queue=collections.deque()
   for tid,name,scope in KNOWN:
    r=conn.execute('SELECT '+keys+' FROM threads WHERE id=?',(tid,)).fetchone()
    if r:
     row=dict(r);row.update(name=name,group=name,kind='对话',parent=None);new[tid]=row;queue.append((tid,name,0))
   while queue and len(new)<120:
    parent,group,depth=queue.popleft()
    if depth>=4:continue
    rows=conn.execute('SELECT '+keys+' FROM threads WHERE source LIKE ? ORDER BY updated_at DESC LIMIT 121',('%'+parent+'%',)).fetchall()
    for raw in rows:
     if len(new)>=120: break
     r=dict(raw)
     try:spawn=json.loads(r['source'])['subagent']['thread_spawn']
     except (ValueError,KeyError,TypeError):continue
     if spawn.get('parent_thread_id')!=parent or r['id'] in new:continue
     r.update(name='子智能体 · '+str(r.get('agent_nickname') or '未命名'),group=group,kind='子智能体',parent=parent)
     new[r['id']]=r;queue.append((r['id'],group,depth+1))
  finally:
   conn.close()
  self.catalog=new;self.catalog_at=time.monotonic()
  if len(new)>=120:self.warnings.append('已达到120条读取上限，清单可能不完整。')
 def snapshot(self):
  now=dt.datetime.now(TZ);self.warnings=[]
  try:self.plans()
  except (OSError,ValueError,KeyError):self.warnings.append('开发计划暂时无法读取；关联结果保留为待确认。')
  try:self.catalog_refresh()
  except (OSError,sqlite3.Error):self.warnings.append('对话登记暂时无法读取；保留上次清单。')
  current=[];history=[];used={}
  for tid,meta in self.catalog.items():
   cp={};cp_path=CHECKPOINTS/(tid+'.json')
   if cp_path.exists():
    try:
     if cp_path.stat().st_size<=64*1024:cp=json.loads(cp_path.read_text(encoding='utf-8-sig'))
    except (OSError,ValueError):pass
   tail=self.tails.get(tid)
   if tail is None or str(tail.path)!=meta['rollout_path']:
    tail=Tail(meta['rollout_path'],2*1024*1024 if meta['kind']=='对话' else 256*1024);self.tails[tid]=tail
   source_error=False
   try:tail.read()
   except OSError:source_error=True
   public=tail.messages[-1] if tail.messages else {}
   public_at=parse_at(public.get('at'));cp_at=parse_at(cp.get('updated_at'))
   # The current checkpoint is the task, newer public messages can supersede it.
   text=cp.get('task','')
   source_at=cp_at
   if public_at and (not cp_at or public_at>cp_at):text=public['text'];source_at=public_at
   if not text:text='暂时没有可读的工作说明'
   brief,ids,validation=describe(text)
   context_used=False
   if not ids and public and public.get('text')!=text:
    turn_start=parse_at(tail.event.get('at')) if tail.event.get('kind')=='task_started' else None
    if not turn_start or (public_at and public_at>=turn_start):
     contextual=describe(text+'\n'+public['text'])
     if contextual[1]:brief,ids,validation=contextual;context_used=True
   if not ids:
    # An implementation-progress note may omit the subject. Keep the recorded context visible.
    candidates=[m['text'] for m in reversed(tail.messages) if m['text']!=text]
    if cp.get('task') and cp['task']!=text:candidates.append(cp['task'])
    for candidate in candidates:
     previous=describe(candidate)
     if previous[1]:
      ids=previous[1];context_used=True
      brief=previous[0]+'（本条进展见下方详情）'
      validation='计划依据最近工作上下文关联，当前细节见原始说明'
      break
   status,state=status_from(cp,tail.event,public,now)
   event_at=parse_at(tail.event.get('at'))
   # A fresh start without a new work description is not evidence the previous job resumed.
   if state=='working' and event_at and source_at and event_at>source_at+dt.timedelta(seconds=1):
    brief='新一轮已开始，具体工作内容还没有更新';validation='当前工作待更新；所列计划是上一条记录的关联';context_used=True;source_at=event_at
   matched=[]
   for item in ids:
    if item in self.plan:
     task=self.plan[item];matched.append({'id':item,'title':FRIENDLY_PLAN.get(item,task['title']),'original_title':task['title'],'status':task['status'],'acceptance':task['acceptance']});used[item]=matched[-1]
   if matched:
    plan_label='\n'.join(p['id']+'\n'+p['title'] for p in matched)
    association=('按最近工作上下文关联，当前记录未明确写计划编号。' if context_used else '按记录内容关联。')+' 不改动原计划状态或派工关系。'
   else:plan_label='待关联到开发计划';association='现有记录不足，尚未确定对应条目。'
   age=int(max(0,(now-source_at).total_seconds()/60)) if source_at else None
   updated=source_at.strftime('%m-%d %H:%M') if source_at else '未记录'
   if age is not None:updated+='\n'+(str(age)+'分钟前' if age<60 else str(age//60)+'小时'+str(age%60)+'分前')
   raw='最近工作记录：\n'+text
   if public and public.get('text')!=text:raw+='\n\n最近公开说明：\n'+public['text']
   if cp:raw+='\n\n检查点中的下一步：\n'+'\n'.join(cp.get('next_actions',[]))
   row={'id':tid,'name':meta['name'],'group':meta['group'],'kind':meta['kind'],'agent_path':meta.get('agent_path') or '',
        'plan':plan_label,'plan_entries':matched,'association':association,'work':brief,'validation':validation,
        'status':status,'state':state,'updated':updated,'source_at':source_at.isoformat() if source_at else '',
        'raw':raw,'source_error':source_error,'parent':meta['parent']}
   last=max([x for x in [source_at,event_at] if x],default=None)
   if meta['kind']=='对话' or (last and last.date()==now.date()):current.append(row)
   else:history.append(row)
  order={tid:i for i,(tid,_,_) in enumerate(KNOWN)}
  current.sort(key=lambda x:(0,order[x['id']]) if x['id'] in order else (1,x['group'],x['name']))
  history.sort(key=lambda x:x['source_at'],reverse=True)
  return {'at':now.isoformat(),'rows':current,'history':history,'plans':list(used.values()),'warnings':self.warnings,
          'catalog_mode':self.catalog_mode,'known_count':len(KNOWN),'agent_count':sum(r['kind']=='子智能体' for r in current),
          'history_count':len(history),'policy':'显示最新记录，不以最近写文件推断正在编码；已交付、暂停与本轮结束分开。'}

def main():
 parser = argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--config', required=True, help='Explicit machine-local project profile JSON')
 parser.add_argument('--once', action='store_true', help='Print one read-only snapshot')
 args = parser.parse_args()
 collector = Collector(args.config)
 if args.once:
  print(json.dumps(collector.snapshot(), ensure_ascii=False)); return
 for command in sys.stdin:
  if command.strip() == 'quit': break
  try: result = collector.snapshot()
  except Exception as e: result = {'error': type(e).__name__, 'message': '暂时无法读取分工记录。'}
  print(json.dumps(result, ensure_ascii=False), flush=True)
if __name__ == '__main__': main()
