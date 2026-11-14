import json,re,sys,os,glob
# Вакансии из писем-рассылок: отсев по названию по правилам сканера, сверка с таблицей и с файлом сканера.
# Вход: /tmp/scan/cand_m*.jsonl (от помощников-писем), today.json, index_keys.txt. Выход: /tmp/scan/mail_cands.jsonl и счётчики.
sys.path.insert(0,'/tmp/scan')
import dedupe as D
PAT=(('li',r'linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*-)?(\d{6,})'),('xing',r'xing\.com/jobs/[^?#]*?-(\d{6,})(?:[/?#]|$)'),
     ('indeed',r'[?&]jk=([0-9a-f]{8,})'),('stst',r'stepstone\.de/[^?#]*?-(\d{6,})-inline'))
def ids(u):
  out=set()
  for k,p in PAT:
    m=re.search(p,u or '',re.I)
    if m:out.add(k+':'+m.group(1).lower())
  return out
def load(p,default):
  try:return json.load(open(p,encoding='utf-8'))
  except Exception:return default
def make_tier(f):
  rx=lambda k:re.compile(f[k],re.I) if f.get(k) else None
  EX,PR,AD,CO,SO=rx('title_exclude'),rx('title_prefer'),rx('title_adjacent'),rx('title_core'),rx('title_soft')
  def tier(t):
    t=t or ''
    hits=[m.group(0) for m in EX.finditer(t)] if EX else []
    if hits:
      jr=bool(re.search(r'junior',t,re.I))
      soft=all((SO and SO.fullmatch(h)) or (jr and re.match(r'senior|sr\.',h,re.I)) for h in hits)
      return 'soft' if soft and PR and PR.search(t) else 'drop'
    if AD and AD.search(t) and not (CO and CO.search(t)):return 'drop'
    return 'prefer' if PR and PR.search(t) else 'other'
  return tier
def run(cands='/tmp/scan/cand_m*.jsonl',today='/tmp/scan/today.json',keys='/tmp/scan/index_keys.txt',out='/tmp/scan/mail_cands.jsonl'):
  t=load(today,{});tier=make_tier(t.get('filters') or {})
  recs=[dict(r) for r in t.get('boards') or []]
  recs+=[dict(r,source='Jobbörse') for r in (t.get('candidates') or [])+(t.get('other') or [])]
  for v in (t.get('watch') or {}).values():
    recs+=[dict(j,company=j.get('company') or v.get('name')) for j in v.get('jobs') or []]
  byid={};byt={}
  for r in recs:
    for i in ids(r.get('url')):byid.setdefault(i,r)
    byt.setdefault(D.norm_title(r.get('title')),[]).append(r)
  known=set(open(keys,encoding='utf-8').read().split()) if os.path.exists(keys) else set()
  kids=set()
  for k in known:kids|=ids(k)
  st={'mail_total':0,'unique':0,'dropped_title':0,'known':0,'soft':0,'in_scanner':0,'with_text':0,'need_copy':0,'by_source':{}}
  seen={};res=[]
  for f in sorted(glob.glob(cands)):
    for l in open(f,encoding='utf-8'):
      try:c=json.loads(l)
      except Exception:continue
      if not isinstance(c,dict) or not c.get('title'):continue
      st['mail_total']+=1
      ci=ids(c.get('url'));key=next(iter(sorted(ci)),None) or (D.norm_title(c.get('title')),D.norm_company(c.get('company')))
      if key in seen:
        o=seen[key]
        if c.get('msgId') and c['msgId'] not in o['msgIds']:o['msgIds'].append(c['msgId'])
        continue
      c['msgIds']=[c.pop('msgId')] if c.get('msgId') else []
      seen[key]=c;st['unique']+=1
      c['tier']=tier(c.get('title'))
      if c['tier']=='drop':st['dropped_title']+=1;continue
      bs=st['by_source'].setdefault(c.get('source') or '?',{'kept':0,'known':0,'in_scanner':0});bs['kept']+=1
      if (c.get('url') and D.norm_url(c['url']) in known) or (ci&kids):st['known']+=1;bs['known']+=1;continue
      if c['tier']=='soft':st['soft']+=1
      r=next((byid[i] for i in sorted(ci) if i in byid),None)
      if r is None:
        r=next((x for x in byt.get(D.norm_title(c.get('title')),[]) if D.same_company(c.get('company'),x.get('company'))),None)
      if r is not None:
        st['in_scanner']+=1;bs['in_scanner']+=1;c['matched']=True;c['scanSource']=r.get('source')
        if r.get('url') and r['url']!=c.get('url'):c['otherUrls']=[c.get('url')] if c.get('url') else [];c['url']=r['url']
        for k in ('refnr','published','km','homeoffice'):
          if r.get(k) is not None:c[k]=r[k]
        if r.get('description'):c['description']=r['description'];st['with_text']+=1
      if not c.get('description'):st['need_copy']+=1
      res.append(c)
  with open(out,'w',encoding='utf-8') as fo:
    for c in res:fo.write(json.dumps(c,ensure_ascii=False)+'\n')
  print(json.dumps(st,ensure_ascii=False))
if __name__=='__main__':run(*sys.argv[1:])
