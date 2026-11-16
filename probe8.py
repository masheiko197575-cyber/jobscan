"""probe 8: Workday — Gesamtzahl je Seite (total auf Seite 1 und 2), Facetten (Land/Firma), 429-Kopfzeilen.
Eine Anfrage alle 3 s, nur öffentliche Listen-API. Ergebnis: data/probe/probe8.json"""
import json, os, re, time, requests

UA = {"User-Agent": "jobscan (+https://github.com/masheiko197575-cyber/jobscan)", "Accept": "application/json"}
cfg = json.load(open("config.json", encoding="utf-8"))
out = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sites": []}
RX = re.compile(r"German|Deutschland|Solar|Storage|Speicher|Energy|Power", re.I)
last = [0.0]


def post(url, body):
    gap = 3.0 - (time.time() - last[0])
    if gap > 0:
        time.sleep(gap)
    last[0] = time.time()
    t = time.time()
    try:
        r = requests.post(url, json=body, headers={**UA, "Content-Type": "application/json"}, timeout=40)
    except Exception as ex:  # noqa: BLE001
        return None, {"error": type(ex).__name__}
    info = {"status": r.status_code, "ms": int((time.time() - t) * 1000),
            "hdr": {k: v for k, v in r.headers.items() if re.search(r"retry|rate|limit|server|cf-|akamai|x-wd", k, re.I)}}
    try:
        return (r.json() if r.status_code == 200 else None), info
    except ValueError:
        return None, info


def facets_summary(d):
    res = []

    def walk(node, param, path):
        vals = node.get("values") or []
        flat = [v for v in vals if isinstance(v, dict) and not v.get("values")]
        if flat:
            res.append({"param": param, "path": path,
                        "top": [f"{v.get('descriptor')}:{v.get('count')}" for v in sorted(flat, key=lambda v: -(v.get('count') or 0))[:6]],
                        "match": [f"{v.get('descriptor')}:{v.get('count')}" for v in flat if RX.search(v.get('descriptor') or '')][:12],
                        "n": len(flat)})
        for v in vals:
            if isinstance(v, dict) and v.get("values"):
                walk(v, v.get("facetParameter") or param, path + "/" + str(v.get("descriptor")))
    for f in d.get("facets") or []:
        walk(f, f.get("facetParameter"), str(f.get("descriptor")))
    return res


seen = set()
for e in cfg.get("workday", []):
    key = (e["host"], e["tenant"], e["site"])
    if key in seen:
        continue
    seen.add(key)
    url = f"https://{e['host']}/wday/cxs/{e['tenant']}/{e['site']}/jobs"
    rec = {"id": e["id"], "site": e["site"]}
    d1, i1 = post(url, {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""})
    rec["p1"] = i1
    if d1:
        rec["total_p1"] = d1.get("total")
        rec["facets"] = facets_summary(d1)
        d2, i2 = post(url, {"appliedFacets": {}, "limit": 20, "offset": 20, "searchText": ""})
        rec["p2"] = i2
        if d2 is not None:
            rec["total_p2"] = d2.get("total")
            rec["n_p2"] = len(d2.get("jobPostings") or [])
    out["sites"].append(rec)
    print(json.dumps({k: rec.get(k) for k in ("id", "site", "total_p1", "total_p2", "n_p2")}, ensure_ascii=False),
          i1.get("status"), (rec.get("p2") or {}).get("status"), flush=True)

out["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
os.makedirs("data/probe", exist_ok=True)
json.dump(out, open("data/probe/probe8.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
