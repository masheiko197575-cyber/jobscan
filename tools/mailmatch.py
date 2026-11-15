import json,re,sys,os,glob
# Вакансии из писем-рассылок: отсев по названию по правилам сканера, сверка с таблицей и с файлом сканера.
# Вход: /tmp/scan/cand_m*.jsonl (от помощников-писем), today.json, index_keys.txt, index.json.
# Формат файла помощника: перед вакансиями каждого сообщения — строка {"msgId","source","word"}, затем по строке на вакансию
# ["должность","компания","город","ссылка"]; ссылка — li:<номер>, jk:<номер>, адрес до «?» или null. Строки-объекты
# {title,company,location,url,source,foundBy,msgId} тоже принимаются.
# Выход: /tmp/scan/mail_cands.jsonl, счётчики в /tmp/scan/mail_stats.json и на экран
# (bad_lines — строки, которые не разобрались: они в /tmp/scan/mail_bad.txt).
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
TRACK=re.compile(r'^(utm_|trk|ref|mid|otp|lipi$|eid$|fbclid$|gclid$|mc_|tk$|qd$|rd$|alid$|bb$|from$|xpse$|xfps$|sc_|cid$|ecid$)',re.I)
def clean(u):
  """Ссылка из письма без хвоста отслеживания: по номеру вакансии — короткий адрес сайта, иначе без служебных параметров."""
  u=(u if isinstance(u,str) else '').strip()
  m=re.fullmatch(r'li:(\d{6,})',u)
  if m:return 'https://www.linkedin.com/jobs/view/'+m.group(1)
  m=re.fullmatch(r'jk:([0-9a-fA-F]{8,})',u)
  if m:return 'https://de.indeed.com/viewjob?jk='+m.group(1).lower()
  if not u.lower().startswith('http'):return ''
  for k,p in PAT:
    m=re.search(p,u,re.I)
    if not m:continue
    if k=='li':return 'https://www.linkedin.com/jobs/view/'+m.group(1)
    if k=='indeed':return 'https://de.indeed.com/viewjob?jk='+m.group(1).lower()
    return re.sub(r'[?#].*$','',u)
  if '?' not in u:return re.sub(r'#.*$','',u)
  head,q=u.split('?',1);q=re.sub(r'#.*$','',q)
  keep=[x for x in q.split('&') if x and not TRACK.match(x.split('=')[0])]
  return head+('?'+'&'.join(keep) if keep else '')
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
def rows_of(p):
  """Индекс таблицы (/tmp/scan/index.json): список строк или словарь id → строка."""
  d=load(p,None)
  if isinstance(d,dict):
    for k in ('rows','jobs','items'):
      if isinstance(d.get(k),list):d=d[k];break
    else:d=[dict(v,id=v.get('id') or k) for k,v in d.items() if isinstance(v,dict)]
  return [r for r in d if isinstance(r,dict)] if isinstance(d,list) else []
def stale_host(t):
  hs=[h.lower() for h in ((t.get('filters') or {}).get('stale_hosts') or [])]
  def f(u):
    h=re.sub(r'^https?://(www\.)?','',u or '').split('/')[0].lower()
    return any(h==x or h.endswith('.'+x) for x in hs)
  return f
def run(cands='/tmp/scan/cand_m*.jsonl',today='/tmp/scan/today.json',keys='/tmp/scan/index_keys.txt',out='/tmp/scan/mail_cands.jsonl',index='/tmp/scan/index.json'):
  t=load(today,{});tier=make_tier(t.get('filters') or {});stale=stale_host(t)
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
  rows=rows_of(index)
  st={'mail_total':0,'unique':0,'dropped_title':0,'known':0,'soft':0,'in_scanner':0,'with_text':0,'need_copy':0,'no_link':0,
      'bad_lines':0,'table_rows':len(rows),'by_source':{}}
  seen={};res=[];bad=[];mails={}
  for f in sorted(glob.glob(cands)):
    hdr={}
    for l in open(f,encoding='utf-8'):
      if not l.strip():continue
      try:c=json.loads(l)
      except Exception:c=None
      if isinstance(c,dict) and c.get('msgId') and not c.get('title'):
        hdr={'msgId':str(c['msgId']),'source':c.get('source'),'word':c.get('word')};continue
      if isinstance(c,list) and c and isinstance(c[0],str) and c[0].strip():
        c=dict(zip(('title','company','location','url'),c),source=hdr.get('source'),msgId=hdr.get('msgId'),
               foundBy='Рассылка %s: %s'%(hdr.get('source') or '?',hdr.get('word') or '?'))
      if not isinstance(c,dict) or not c.get('title'):
        st['bad_lines']+=1;bad.append(l.rstrip('\n')[:400]);continue
      st['mail_total']+=1
      src=c.get('source') or '?'
      bs=st['by_source'].setdefault(src,{'mails':0,'total':0,'kept':0,'known':0,'in_scanner':0});bs['total']+=1
      if c.get('msgId'):mails.setdefault(src,set()).add(c['msgId'])
      c['url']=clean(c.get('url'))
      ci=ids(c.get('url'));key=next(iter(sorted(ci)),None) or (D.norm_title(c.get('title')),D.norm_company(c.get('company')))
      if key in seen:
        o=seen[key]
        if c.get('msgId') and c['msgId'] not in o['msgIds']:o['msgIds'].append(c['msgId'])
        continue
      c['msgIds']=[c.pop('msgId')] if c.get('msgId') else []
      seen[key]=c;st['unique']+=1
      c['tier']=tier(c.get('title'))
      if c['tier']=='drop':st['dropped_title']+=1;continue
      bs['kept']+=1
      if (c.get('url') and D.norm_url(c['url']) in known) or (ci&kids):st['known']+=1;bs['known']+=1;continue
      tab=None
      for r in rows:
        try:x=D.compare(c,r)
        except Exception:x=None
        if x in ('same','repost','other-city'):tab=(x,r.get('id'));break
        if x=='check' and tab is None:tab=(x,r.get('id'))
      if tab and tab[0]!='check':st['known']+=1;bs['known']+=1
      if tab and tab[0]=='same':continue
      if tab:c['table'],c['tableId']=tab
      if c['tier']=='soft':st['soft']+=1
      if not c.get('url'):st['no_link']+=1
      r=next((byid[i] for i in sorted(ci) if i in byid),None)
      if r is None:
        r=next((x for x in byt.get(D.norm_title(c.get('title')),[]) if D.same_company(c.get('company'),x.get('company'))),None)
      if r is not None:
        st['in_scanner']+=1;bs['in_scanner']+=1;c['matched']=True;c['scanSource']=r.get('source')
        if r.get('url') and r['url']!=c.get('url') and not (stale(r['url']) and c.get('url')):
          c['otherUrls']=[c['url']] if c.get('url') and not (ci&ids(r['url'])) else [];c['url']=r['url']
        if r.get('verify') and stale(c.get('url')):c['verify']=True
        for k in ('refnr','published','km','homeoffice'):
          if r.get(k) is not None:c[k]=r[k]
        if r.get('description'):c['description']=r['description'];st['with_text']+=1
      if not c.get('description'):st['need_copy']+=1
      res.append(c)
  for src,m in mails.items():st['by_source'][src]['mails']=len(m)
  with open(out,'w',encoding='utf-8') as fo:
    for c in res:fo.write(json.dumps(c,ensure_ascii=False)+'\n')
  d=os.path.dirname(out)
  if bad:open(os.path.join(d,'mail_bad.txt'),'w',encoding='utf-8').write('\n'.join(bad)+'\n')
  json.dump(st,open(os.path.join(d,'mail_stats.json'),'w',encoding='utf-8'),ensure_ascii=False)
  print(json.dumps(st,ensure_ascii=False))
if __name__=='__main__':run(*sys.argv[1:])
