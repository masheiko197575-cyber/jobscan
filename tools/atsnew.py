import json,re,sys,os,glob,datetime as DT
# Шаг 8 утреннего подбора — поиск по сайтам вакансий компаний (ATS).
#   python3 atsnew.py queries — запросы на сегодня: треть списка meta/ats.queries, по кругу за три дня
#   python3 atsnew.py         — /tmp/scan/ats_raw*.jsonl ({title,url,query} от помощника-поиска) без уже просмотренного
#                               (meta/atsSeen.urls), известного таблице (index_keys.txt) и отсеянного по названию
#                               -> /tmp/scan/ats_new.jsonl; все ссылки поиска -> /tmp/scan/ats_urls.json (для meta/atsSeen)
sys.path.insert(0,'/tmp/scan')
import dedupe as D
W='/tmp/scan'
# load и make_tier — те же, что в mailmatch.py (скрипт самостоятельный: mailmatch.py подбор больше не скачивает)
def load(p,default):
  try:return json.load(open(p,encoding='utf-8'))
  except Exception:return default
def make_tier(f):
  rx=lambda k:re.compile(f[k],re.I) if f.get(k) else None
  EX,PR,AD,CO,SO,OP=rx('title_exclude'),rx('title_prefer'),rx('title_adjacent'),rx('title_core'),rx('title_soft'),rx('title_senior_optional')
  def tier(t):
    t=t or ''
    hits=[m.group(0) for m in EX.finditer(t)] if EX else []
    soft=False
    if hits:
      # «Senior» рядом с «Junior» или только как возможность («(Senior) Projektmanager», правило сканера title_senior_optional)
      jr=bool(re.search(r'junior',t,re.I)) or bool(OP and OP.search(t) and not re.search(r'\bsenior|\bsr\.',OP.sub(' ',t),re.I))
      soft=bool(PR and PR.search(t)) and all((SO and SO.fullmatch(h)) or (jr and re.match(r'senior|sr\.',h,re.I)) for h in hits)
      if not soft:return 'drop'
    if AD and AD.search(t) and not (CO and CO.search(t)):return 'drop'
    if soft:return 'soft'
    return 'prefer' if PR and PR.search(t) else 'other'
  return tier
def doc(name,field):
  """Документ meta/<name> из файлов, куда его положил ArtifactData (out_dir): сам документ или обёртка с data."""
  for p in [f'{W}/meta/{name}.json',f'{W}/db/meta/{name}.json']+sorted(glob.glob(f'{W}/**/{name}.json',recursive=True)):
    d=load(p,None)
    if isinstance(d,dict):
      if field in d:return d
      if isinstance(d.get('data'),dict) and field in d['data']:return d['data']
  return {}
def today():
  try:
    from zoneinfo import ZoneInfo
    return DT.datetime.now(ZoneInfo('Europe/Berlin')).date()
  except Exception:
    return (DT.datetime.utcnow()+DT.timedelta(hours=1)).date()
def queries():
  qs=[q for q in doc('ats','queries').get('queries') or [] if isinstance(q,str) and q.strip()]
  n=today().toordinal()%3
  print(json.dumps({'total':len(qs),'part':n,'queries':[q for i,q in enumerate(qs) if i%3==n]},ensure_ascii=False))
def run():
  t=load(f'{W}/today.json',{});tier=make_tier(t.get('filters') or {})
  seen=set()
  for u in doc('atsSeen','urls').get('urls') or []:
    if isinstance(u,str):seen.add(u.strip());seen.add(D.norm_url(u))
  kp=f'{W}/index_keys.txt'
  known=set(open(kp,encoding='utf-8').read().split()) if os.path.exists(kp) else set()
  st={'raw':0,'unique':0,'seen':0,'known':0,'dropped_title':0,'new':0};uniq=set();out=[];urls=[]
  for f in sorted(glob.glob(f'{W}/ats_raw*.jsonl')):
    for l in open(f,encoding='utf-8'):
      try:c=json.loads(l)
      except Exception:continue
      u=(c.get('url') or '').strip() if isinstance(c,dict) else ''
      if not u.startswith('http'):continue
      st['raw']+=1;k=D.norm_url(u)
      if k in uniq:continue
      uniq.add(k);st['unique']+=1;urls.append(u)
      if u in seen or k in seen:st['seen']+=1;continue
      if k in known:st['known']+=1;continue
      c['tier']=tier(c.get('title'))
      if c['tier']=='drop':st['dropped_title']+=1;continue
      c['source']='Сайт компании';c['foundBy']='ATS: '+str(c.get('query') or '?');out.append(c)
  with open(f'{W}/ats_new.jsonl','w',encoding='utf-8') as fo:
    for c in out:fo.write(json.dumps(c,ensure_ascii=False)+'\n')
  json.dump(urls,open(f'{W}/ats_urls.json','w',encoding='utf-8'),ensure_ascii=False)
  st['new']=len(out);print(json.dumps(st,ensure_ascii=False))
if __name__=='__main__':
  queries() if sys.argv[1:2]==['queries'] else run()
