"""Probe 5 (06.11.2026): JOIN — Seitenwechsel (?page=N) zuverlässig? öffentliche API mit gültigem pageSize?
Aktive Stellenseite (Status, Texte, Daten)."""
import json, os, re, time
import requests

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "probe")
RAW = os.path.join(OUT, "raw5")
os.makedirs(RAW, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
      "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}
summary = {}


def nd(t):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', t, re.S)
    if not m:
        return {}
    pp = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
    return pp.get("pageData") or pp.get("initialState") or {}


def page(name, url):
    try:
        r = requests.get(url, headers=UA, timeout=30)
        d = nd(r.text)
        j = d.get("jobs") or {}
        summary[name] = {"status": r.status_code, "cf_cache": r.headers.get("cf-cache-status"), "age": r.headers.get("age"),
                         "cache_control": r.headers.get("cache-control"), "pagination": j.get("pagination"),
                         "ids": [i.get("idParam", "")[:8] for i in j.get("items") or []]}
    except Exception as ex:  # noqa: BLE001
        summary[name] = {"error": repr(ex)[:200]}


for slug in ("axsol", "junizenergy", "encosa615", "solarize"):
    for n in (1, 2, 3):
        page(f"{slug}_p{n}", f"https://join.com/companies/{slug}" + (f"?page={n}" if n > 1 else ""))
        time.sleep(0.5)
    page(f"{slug}_p2_bust", f"https://join.com/companies/{slug}?page=2&t={int(time.time())}")

for cid, slug in ((39626, "axsol"), (15425, "junizenergy")):
    for ps in (5, 10, 20, 25, 30, 40):
        u = f"https://join.com/api/public/companies/{cid}/jobs?page=1&pageSize={ps}"
        try:
            r = requests.get(u, headers={**UA, "Accept": "application/json"}, timeout=30)
            body = r.text
            info = {"status": r.status_code, "len": len(body), "head": body[:300]}
            if r.status_code == 200:
                d = r.json()
                info["keys"] = list(d.keys()) if isinstance(d, dict) else type(d).__name__
                open(os.path.join(RAW, f"api_{slug}_{ps}.json"), "w", encoding="utf-8").write(body[:400000])
            summary[f"api_{slug}_ps{ps}"] = info
        except Exception as ex:  # noqa: BLE001
            summary[f"api_{slug}_ps{ps}"] = {"error": repr(ex)[:200]}
        time.sleep(0.4)

r = requests.get("https://join.com/companies/junizenergy/16874449-projektleiter-m-w-d-batteriegrossspeicher-bess", headers=UA, timeout=30)
d = nd(r.text)
job = d.get("job") or {}
summary["job_live"] = {"status": r.status_code, "final": r.url, "job_status": job.get("status"), "createdAt": job.get("createdAt"),
                       "updatedAt": job.get("updatedAt"), "closedAt": job.get("closedAt"), "lastId": job.get("lastId"),
                       "idParam": job.get("idParam"), "workplaceType": job.get("workplaceType"), "city": job.get("city"),
                       "salary": {k: job.get(k) for k in job if "alary" in k},
                       "desc_len": len(job.get("description") or ""), "keys": sorted(job.keys())}
open(os.path.join(RAW, "job_live.json"), "w", encoding="utf-8").write(json.dumps(job, ensure_ascii=False, indent=1)[:200000])
json.dump(summary, open(os.path.join(OUT, "summary5.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(json.dumps(summary, ensure_ascii=False, indent=1)[:8000])
