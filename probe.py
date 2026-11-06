"""Probe 6 (06.11.2026): Personio-Feeds, die 404 liefern (Bürgerwerke, Energiegewinner, Energieversum, TAUBER-SOLAR,
trawa) und ju:niz bei Personio — welche Adresse liefert die Stellenliste?"""
import json, os, re
import requests

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "probe")
RAW = os.path.join(OUT, "raw6")
os.makedirs(RAW, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) jobscan/1.0"}
UA_BROWSER = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
              "Accept-Language": "de-DE,de;q=0.9"}
summary = {}
for slug in ("buergerwerke", "energiegewinner-egt", "energieversum", "tauber-solar", "tauber-energy", "trawa", "juniz-energy", "arteus-energy"):
    for dom in ("de", "com"):
        for path, hdr in (("/xml", UA), ("/xml?language=de", UA), ("/xml", UA_BROWSER), ("/", UA_BROWSER), ("/search.json", UA_BROWSER)):
            url = f"https://{slug}.jobs.personio.{dom}{path}"
            key = f"{slug}|{dom}|{path}|{'ua' if hdr is UA else 'browser'}"
            try:
                r = requests.get(url, headers=hdr, timeout=30, allow_redirects=True)
                t = r.text
                info = {"status": r.status_code, "final": r.url, "ctype": (r.headers.get("content-type") or "")[:40], "len": len(t),
                        "positions": t.count("<position>"), "job_links": len(set(re.findall(r"/job/(\d+)", t))),
                        "title": ((re.search(r"<title[^>]*>(.*?)</title>", t, re.S | re.I) or [None, ""])[1] or "")[:80].strip()}
                if r.status_code == 200 and (info["positions"] or info["job_links"] or "json" in info["ctype"]):
                    fn = re.sub(r"[^a-z0-9]+", "_", key.lower())[:80]
                    open(os.path.join(RAW, fn + (".xml" if info["positions"] else ".txt")), "w", encoding="utf-8").write(t[:300000])
                summary[key] = info
            except Exception as ex:  # noqa: BLE001
                summary[key] = {"error": repr(ex)[:200]}
json.dump(summary, open(os.path.join(OUT, "summary6.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
for k, v in summary.items():
    print(k, json.dumps(v, ensure_ascii=False)[:220])
