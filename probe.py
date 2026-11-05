"""Test 3: Glassdoor — ist es unser Aufruf oder eine Sperre des GitHub-Servers?
1) Beispiel aus der JobSpy-README unverändert (USA), 2) Deutschland (Berlin), 3) einfache GET-Anfragen
an glassdoor.com/.de mit Status und Kopfzeilen (Cloudflare?)."""
import io, json, logging, os, re, traceback
import requests

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "probe")
os.makedirs(OUT, exist_ok=True)
summary = {}
buf = io.StringIO()
logging.getLogger().addHandler(logging.StreamHandler(buf))
logging.getLogger().setLevel(logging.INFO)
from jobspy import scrape_jobs
import jobspy
summary["jobspy_version"] = getattr(jobspy, "__version__", "?")
cases = {"readme_usa": dict(site_name=["glassdoor"], search_term="software engineer", location="San Francisco, CA",
                            results_wanted=20, hours_old=72, country_indeed="USA"),
         "germany_berlin": dict(site_name=["glassdoor"], search_term="Projektmanager Photovoltaik", location="Berlin",
                                results_wanted=20, country_indeed="Germany")}
for name, kw in cases.items():
    try:
        df = scrape_jobs(verbose=2, **kw)
        rows = [] if df is None else df.to_dict("records")
        summary[name] = {"count": len(rows), "sample": [{k: str(r.get(k))[:70] for k in ("title", "company", "location")} for r in rows[:3]]}
    except Exception:  # noqa: BLE001
        summary[name] = {"error": traceback.format_exc()[-400:]}
summary["jobspy_log"] = buf.getvalue()[-2500:]
ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
for name, url in {"com_autocomplete": "https://www.glassdoor.com/autocomplete/location?locationTypeFilters=CITY,STATE,COUNTRY&caller=jobs&term=Berlin",
                  "de_autocomplete": "https://www.glassdoor.de/autocomplete/location?locationTypeFilters=CITY,STATE,COUNTRY&caller=jobs&term=Berlin",
                  "de_home": "https://www.glassdoor.de/index.htm"}.items():
    try:
        r = requests.get(url, headers=ua, timeout=25)
        t = r.text
        summary[name] = {"status": r.status_code, "server": r.headers.get("server"), "cf-ray": r.headers.get("cf-ray"),
                         "cf-mitigated": r.headers.get("cf-mitigated"),
                         "title": (re.search(r"<title[^>]*>(.*?)</title>", t, re.S | re.I) or [None, None])[1],
                         "snippet": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t))[:200]}
    except Exception as ex:  # noqa: BLE001
        summary[name] = {"error": repr(ex)[:200]}
json.dump(summary, open(os.path.join(OUT, "summary3.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(json.dumps(summary, ensure_ascii=False, indent=1)[:6000])
