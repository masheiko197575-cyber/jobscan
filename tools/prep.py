import json,re,sys,os,glob,math,time,collections as C,datetime as DT
# Отбор новых вакансий из файла сканера для утреннего подбора — механическая часть шагов 7–10, которую главный агент
# каждый день программировал заново: окно по времени, уже известное таблице, сверка дублей, волны.
#   python3 prep.py shadow — «теневой» режим: считает отбор, сравнивает его с тем, что агент в этом запуске записал сам,
#                            и печатает итог одной строкой JSON (tokens.py кладёт его в журнал запуска:
#                            parts.main.prep_shadow). В работе агента ничего не меняет.
#   python3 prep.py        — только отбор.
# Файлы отбора — в /tmp/scan/prep/: w1.jsonl, w2.jsonl, w3.jsonl (кандидаты по волнам, с текстом из файла сканера),
# glance.txt (названия без слова роли — решает агент), checks.txt (похожие на строки таблицы), stats.json.
# Правило: скрипт убирает только однозначное (уже в таблице, вне окна, компания на паузе); всё остальное отдаёт агенту.
W=os.environ.get('PREP_BASE','/tmp/scan')
sys.path.insert(0,W)
import dedupe as D
PAT=(('li',r'linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*-)?(\d{6,})'),('xing',r'xing\.com/jobs/[^?#]*?-(\d{6,})(?:[/?#]|$)'),
     ('indeed',r'[?&]jk=([0-9a-f]{8,})'),('stst',r'stepstone\.de/[^?#]*?-(\d{6,})-inline'))
def ids(u):
  """Номер вакансии в ссылке LinkedIn, XING, Indeed, StepStone: одна и та же вакансия приходит по разным адресам."""
  out=set()
  for k,p in PAT:
    m=re.search(p,u if isinstance(u,str) else '',re.I)
    if m:out.add(k+':'+m.group(1).lower())
  return out
TRACK=re.compile(r'^(utm_|trk|fbclid$|gclid$|mc_|gh_src$|lipi$|tk$|from$|xpse$|xfps$|bb$|sc_|source$|src$|lang$|language$|locale$|hl$)',re.I)
def ukey(u):
  """Ключ ссылки — то же правило, что norm_url модуля сверки таблицы с 25.11.2026 (своя копия, чтобы отбор не зависел от
  версии модуля): номер вакансии в самом адресе (5+ цифр подряд) — параметры не нужны; иначе номер может стоять в
  параметрах (Indeed ?jk=, eejobs ?id=, hrworks ?id=), и они остаются, кроме меток отслеживания. Прежний norm_url отрезал
  всё после «?», и все ссылки таких сайтов сливались в одну — «уже в таблице» выходило бы у каждой."""
  u=(u if isinstance(u,str) else '').strip()
  if not u.lower().startswith('http'):return ''
  m=re.match(r'https?://[^/?#]*indeed\.[^/?#]*/[^#]*[?&]jk=([0-9a-f]{8,})',u,re.I)
  if m:return 'https://de.indeed.com/viewjob?jk='+m.group(1).lower()
  head,_,q=re.sub(r'#.*$','',u).lower().partition('?');head=head.rstrip('/')
  if not q or re.search(r'\d{5,}',re.sub(r'^https?://[^/]+','',head)):return head
  keep=sorted(x for x in q.split('&') if x and not TRACK.match(x.split('=')[0]))
  return head+('?'+'&'.join(keep) if keep else '')
def links(r):
  return [u for u in [r.get('url'),r.get('external_url')]+list(r.get('otherUrls') or []) if isinstance(u,str) and u]
def rowkeys(r):
  """Ключи строки таблицы: refnr, id и номер карточки Jobbörse — как в модуле сверки; ссылки — по ukey; номера вакансий досок."""
  ks=set()
  try:ks={str(k) for k in D.keys(r) if not str(k).lower().startswith('http')}
  except Exception:pass
  for u in links(r):
    k=ukey(u)
    if k:ks.add(k)
    ks|=ids(u)
  return ks
def same_or(c,r,rk):
  """Сверка кандидата со строкой: та же ссылка или номер — 'same'; иначе правила модуля по компании, названию и городу."""
  if c.get('refnr') and str(c['refnr']) in rk:return 'same'
  for u in links(c):
    if ukey(u) in rk or (ids(u)&rk):return 'same'
  try:return D.compare(dict(c,url=None,refnr=None),r)
  except Exception:return None
def load(p,default):
  try:return json.load(open(p,encoding='utf-8'))
  except Exception:return default
def make_tier(f):
  """Отсев по названию по правилам сканера (today.json → filters) — как в сканере."""
  rx=lambda k:re.compile(f[k],re.I) if f.get(k) else None
  EX,PR,AD,CO,SO,OP=rx('title_exclude'),rx('title_prefer'),rx('title_adjacent'),rx('title_core'),rx('title_soft'),rx('title_senior_optional')
  def tier(t):
    t=t or ''
    hits=[m.group(0) for m in EX.finditer(t)] if EX else []
    soft=False
    if hits:
      jr=bool(re.search(r'junior',t,re.I)) or bool(OP and OP.search(t) and not re.search(r'\bsenior|\bsr\.',OP.sub(' ',t),re.I))
      soft=bool(PR and PR.search(t)) and all((SO and SO.fullmatch(h)) or (jr and re.match(r'senior|sr\.',h,re.I)) for h in hits)
      if not soft:return 'drop'
    if AD and AD.search(t) and not (CO and CO.search(t)):return 'drop'
    if soft:return 'soft'
    return 'prefer' if PR and PR.search(t) else 'other'
  return tier
def unwrap(x,name):
  """Документ(ы) из файла, куда их положил ArtifactData (out_dir): сам документ, обёртка с data или список."""
  if isinstance(x,list):
    for y in x:yield from unwrap(y,None)
  elif isinstance(x,dict):
    inner=next((x[k] for k in('documents','docs','results','items','rows') if isinstance(x.get(k),list)),None)
    if isinstance(x.get('data'),dict) and any(k in x for k in('id','doc_id','version','updatedAt')):
      yield dict(x['data'],id=x.get('id') or x.get('doc_id') or name)
    elif inner is not None and not any(k in x for k in('title','name','url')):
      yield from unwrap(inner,None)
    elif any(k in x for k in('title','company','name','url','careersUrl')):
      yield dict(x,id=x.get('id') or name)
def docs_in(d):
  """Все документы из папки out_dir, по одному на id (если документ лежит в нескольких файлах — последний)."""
  out={}
  for p in sorted(glob.glob(d+'/**/*.json',recursive=True))+sorted(glob.glob(d+'/**/*.jsonl',recursive=True)):
    name=os.path.splitext(os.path.basename(p))[0]
    try:txt=open(p,encoding='utf-8').read()
    except Exception:continue
    try:items=[json.loads(txt)]
    except Exception:
      items=[]
      for l in txt.splitlines():
        try:items.append(json.loads(l))
        except Exception:pass
    for x in items:
      for r in unwrap(x,name):
        if r.get('id'):out[str(r['id'])]=r
  return list(out.values())
def rows_of(p):
  """Индекс таблицы, который строит агент (/tmp/scan/index.json): список строк или словарь id → строка."""
  d=load(p,None)
  if isinstance(d,dict):
    for k in('rows','jobs','items'):
      if isinstance(d.get(k),list):d=d[k];break
    else:d=[dict(v,id=v.get('id') or k) for k,v in d.items() if isinstance(v,dict)]
  return [r for r in d if isinstance(r,dict)] if isinstance(d,list) else []
def doc(name,field):
  """Документ meta/<name> из файлов out_dir: сам документ или обёртка с data."""
  for p in [f'{W}/meta/{name}.json',f'{W}/db/meta/{name}.json']+sorted(glob.glob(f'{W}/**/{name}.json',recursive=True)):
    d=load(p,None)
    if isinstance(d,dict):
      if field in d:return d
      if isinstance(d.get('data'),dict) and field in d['data']:return d['data']
  return {}
def ts(s):
  """Время из файла сканера или журнала запуска (ISO, UTC) → datetime; не разобралось — None."""
  if not isinstance(s,str) or len(s)<10:return None
  try:
    d=DT.datetime.fromisoformat(s.strip().replace('Z','+00:00'))
    return d if d.tzinfo else d.replace(tzinfo=DT.timezone.utc)
  except Exception:return None
def berlin(d):
  try:
    from zoneinfo import ZoneInfo
    return d.astimezone(ZoneInfo('Europe/Berlin')).date()
  except Exception:
    return (d.astimezone(DT.timezone.utc)+DT.timedelta(hours=1)).date()
def keyset(rows):
  """Ключи так, как их считает модуль сверки (для сравнения с index_keys.txt агента)."""
  K=set()
  for r in rows:
    try:K|={str(k) for k in D.keys(r)}
    except Exception:pass
  return K
def select():
  """Каждой записи файла сканера — решение: волна (w1–w3), «на взгляд агента» (g1–g3), повтор (dup) или убрана (drop + причина)."""
  t=load(W+'/today.json',{})
  sp=W+'/start'
  now=DT.datetime.fromtimestamp(os.path.getmtime(sp),DT.timezone.utc) if os.path.exists(sp) else DT.datetime.now(DT.timezone.utc)
  last=ts(doc('lastRun','at').get('at'))
  days=min(7,max(1,math.ceil((now-last).total_seconds()/86400))) if last else 3
  since=last or now-DT.timedelta(days=3)
  cut=(berlin(now)-DT.timedelta(days=days)).isoformat()
  db=[r for r in docs_in(W+'/db') if r.get('title')]
  agent=rows_of(W+'/index.json')
  rows=db or agent
  RK=[rowkeys(r) for r in rows];K=set().union(*RK) if RK else set()
  cos=docs_in(W+'/co')
  co={str(c['id']):c for c in cos} if cos else None
  tier=make_tier(t.get('filters') or {})
  info={'days':days,'rows':len(rows),'db_rows':len(db),'agent_rows':len(agent),'companies':len(cos),'date':t.get('date'),'used':'db' if db else 'agent'}
  res=[]
  def put(kind,e,w,why=None):res.append({'k':kind,'e':e,'w':w,'why':why})
  def known(e):
    if e.get('refnr') and str(e['refnr']) in K:return True
    return any(ukey(u) in K or (ids(u)&K) for u in links(e))
  def versus(e):
    tab=(None,None)
    for r,rk in zip(rows,RK):
      x=same_or(e,r,rk)
      if x in('same','repost','other-city'):return x,r.get('id')
      if x=='check' and tab[0] is None:tab=(x,r.get('id'))
    return tab
  def add(kind,e,wave,glance=False):
    if known(e):return put(kind,e,'drop','known')
    x,rid=versus(e)
    if x=='same':return put(kind,e,'drop','known')
    if x:e=dict(e,table=x,tableId=rid)
    put(kind,e,('g' if glance else 'w')+str(wave))
  # отложенные прошлым запуском — первыми
  for it in doc('backlog','items').get('items') or []:
    if isinstance(it,dict) and it.get('title'):add('backlog',dict(it,published=it.get('publishedAt')),1)
  # Jobbörse: candidates (с текстом) и other (только названия)
  for kind,lst in(('cand',t.get('candidates') or []),('other',t.get('other') or [])):
    for c in lst:
      e=dict(c,source='Jobbörse')
      p=str(c.get('published') or '')[:10]
      if p and p<cut:put(kind,e,'drop','old');continue
      near=(isinstance(c.get('km'),(int,float)) and c['km']<=75) or bool(c.get('homeoffice'))
      add(kind,e,2 if kind=='other' or not near else 1,glance=(kind=='other'))
  # компании под наблюдением: только активные из таблицы, только вакансии новее прошлого запуска
  for cid,v in(t.get('watch') or {}).items():
    state=None
    if co is not None:
      c=co.get(str(cid))
      state='no-company' if c is None else 'paused' if c.get('active') is False else 'pending' if c.get('pending') else None
    for j in v.get('jobs') or []:
      e=dict(j,company=j.get('company') or v.get('name'),watch=cid)
      if state:put('watch',e,'drop',state);continue
      fs=ts(j.get('first_seen'))
      if fs and fs<=since:put('watch',e,'drop','seen-before');continue
      add('watch',e,1,glance=((j.get('title_tier') or tier(j.get('title'))) not in('prefer','soft')))
  # доски
  for b in t.get('boards') or []:
    fs=ts(b.get('first_seen'))
    if fs and fs<=since:put('board',b,'drop','seen-before');continue
    add('board',dict(b),3,glance=((b.get('title_tier') or tier(b.get('title'))) not in('prefer','soft')))
  # одна вакансия из нескольких источников: оставить первую по порядку волн, остальные — её повторы
  acc=[]
  order=sorted((i for i,x in enumerate(res) if x['w']!='drop'),key=lambda i:(res[i]['w'][1],res[i]['w'][0]!='w',not res[i]['e'].get('description'),i))
  for i in order:
    e=res[i]['e'];hit=None
    for j in acc:
      a=res[j]['e']
      if same_or(e,a,rowkeys(a)) in('same','repost','other-city'):hit=j;break
    if hit is None:acc.append(i);continue
    a=res[hit]['e']
    if e.get('url') and e['url']!=a.get('url'):a.setdefault('otherUrls',[]).append(e['url'])
    if e.get('location') and e['location']!=a.get('location'):a.setdefault('otherLocations',[]).append(e['location'])
    res[i]['w']='dup';res[i]['of']=hit
  return res,info
def write_files(res,info):
  d=W+'/prep';os.makedirs(d,exist_ok=True)
  n=C.Counter(x['w'] for x in res);why=C.Counter(x['why'] for x in res if x['w']=='drop')
  for w in('w1','w2','w3'):
    with open(f'{d}/{w}.jsonl','w',encoding='utf-8') as fo:
      for x in res:
        if x['w']==w:fo.write(json.dumps(dict(x['e'],kind=x['k']),ensure_ascii=False)+'\n')
  with open(d+'/glance.txt','w',encoding='utf-8') as fo:
    for x in res:
      if x['w'][0]=='g':
        e=x['e'];fo.write('\t'.join([x['w'],str(e.get('refnr') or e.get('id') or e.get('url') or ''),'%s — %s — %s'%(e.get('title'),e.get('company'),e.get('location'))])+'\n')
  with open(d+'/checks.txt','w',encoding='utf-8') as fo:
    for x in res:
      e=x['e']
      if x['w']!='drop' and e.get('table'):
        fo.write('\t'.join([str(e['table']),str(e.get('tableId')),'%s — %s — %s — %s'%(e.get('title'),e.get('company'),e.get('location'),e.get('published') or '')])+'\n')
  st=dict(info,sel={k:n[k] for k in sorted(n) if k!='drop'},drop=dict(why))
  json.dump(st,open(d+'/stats.json','w',encoding='utf-8'),ensure_ascii=False)
  return st
def written(today):
  """Новые документы, которые агент в этом запуске подготовил к записи: ищем их в файлах рабочей папки."""
  sp=W+'/start';start=os.path.getmtime(sp) if os.path.exists(sp) else 0
  found={}
  def walk(x):
    if isinstance(x,dict):
      if x.get('foundAt')==today and x.get('status')=='new' and x.get('title'):
        found.setdefault(ukey(x.get('url')) or (D.norm_title(x.get('title')),D.norm_company(x.get('company'))),x)
      for v in x.values():walk(v)
    elif isinstance(x,list):
      for v in x:walk(v)
  for p in glob.glob(W+'/**/*.json*',recursive=True)+glob.glob('/tmp/*.json*'):
    q=p.replace('\\','/')
    if any(s in q for s in('/db/','/co/','/meta/','/prep/')) or os.path.basename(p) in('today.json','index.json'):continue
    try:
      if os.path.getmtime(p)<start-1 or os.path.getsize(p)>8_000_000:continue
      txt=open(p,encoding='utf-8').read()
    except Exception:continue
    try:walk(json.loads(txt))
    except Exception:
      for l in txt.splitlines():
        try:walk(json.loads(l))
        except Exception:pass
  return list(found.values())
def shadow():
  t0=time.time()
  res,info=select();st=write_files(res,info)
  by_ref,by_url,by_id,by_t=C.defaultdict(set),C.defaultdict(set),C.defaultdict(set),C.defaultdict(set)
  for i,x in enumerate(res):
    e=x['e']
    if e.get('refnr'):by_ref[str(e['refnr'])].add(i)
    for u in links(e):
      by_url[ukey(u)].add(i)
      for k in ids(u):by_id[k].add(i)
    by_t[D.norm_title(e.get('title'))].add(i)
  sp=W+'/start'
  now=DT.datetime.fromtimestamp(os.path.getmtime(sp),DT.timezone.utc) if os.path.exists(sp) else DT.datetime.now(DT.timezone.utc)
  ws=written(berlin(now).isoformat())
  ok=inside=0;lost=[];why=C.Counter()
  for w in ws:
    hit=set()
    if w.get('refnr'):hit|=by_ref.get(str(w['refnr']),set())
    for u in links(w):
      hit|=by_url.get(ukey(u),set())
      for k in ids(u):hit|=by_id.get(k,set())
    if not hit:
      hit={i for i in by_t.get(D.norm_title(w.get('title')),set()) if D.same_company(w.get('company'),res[i]['e'].get('company'))}
    if not hit:continue  # не из файла сканера: добавлено вручную, поиск по сайтам компаний, rejobs.org
    inside+=1
    if any(res[i]['w']!='drop' for i in hit):ok+=1
    else:
      r=sorted(res[i]['why'] or '?' for i in hit)[0];why[r]+=1
      if len(lost)<6:lost.append('%s: %s — %s'%(r,str(w.get('title'))[:60],str(w.get('company'))[:30]))
  # индекс таблицы: мой разбор папки db против индекса агента
  kp=W+'/index_keys.txt'
  ka=set(open(kp,encoding='utf-8').read().split()) if os.path.exists(kp) else set()
  kd=keyset([r for r in docs_in(W+'/db') if r.get('title')])
  out={'v':1,'days':info['days'],'file':info['date'],'sel':st['sel'],'drop':st['drop'],'written':len(ws),'from_scanner':inside,'ok':ok,
       'lost':lost,'lost_why':dict(why),'index':{'db_rows':info['db_rows'],'agent_rows':info['agent_rows'],'companies':info['companies'],
       'keys_db':len(kd),'keys_agent':len(ka),'only_db':len(kd-ka),'only_agent':len(ka-kd),'used':info['used']},'sec':round(time.time()-t0,1)}
  json.dump(out,open(W+'/prep/shadow.json','w',encoding='utf-8'),ensure_ascii=False)
  print(json.dumps(out,ensure_ascii=False))
if __name__=='__main__':
  if sys.argv[1:2]==['shadow']:shadow()
  else:print(json.dumps(write_files(*select()),ensure_ascii=False))
