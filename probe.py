"""Einmaliger Test: liefern StepStone, XING und Glassdoor (JobSpy) Ergebnisse an einen GitHub-Server?
Schreibt Rohantworten und eine Zusammenfassung nach data/probe/ (Branch probe-out)."""
import json, os, re, time, traceback
import requests

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "probe")
os.makedirs(OUT, exist_ok=True)
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
     "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}
S = requests.Session()
summary = {}


def get(name, url, **kw):
    rec = {"url": url}
    try:
        r = S.get(url, headers=H, timeout=40, **kw)
        t = r.text
        rec.update(status=r.status_code, final=r.url, len=len(t), ctype=r.headers.get("content-type"),
                   title=(re.search(r"<title[^>]*>(.*?)</title>", t, re.S | re.I) or [None, None])[1],
                   markers={m: t.count(m) for m in ("__PRELOADED_STATE__", "__NEXT_DATA__", "unifiedResultlist", "searchResults",
                                                    "stellenangebote--", "/jobs/", "jobPosting", "JobPosting", "captcha", "Access Denied",
                                                    "akamai", "cf-chl", "window.__", "application/ld+json", "graphql")})
        with open(os.path.join(OUT, f"{name}.html"), "w", encoding="utf-8") as f:
            f.write(t[:2_000_000])
        summary[name] = rec
        return t
    except Exception as ex:  # noqa: BLE001
        rec["error"] = repr(ex)[:300]
        summary[name] = rec
        return ""


# StepStone: Suchseite (zwei URL-Formen) und eine Stellenseite
t = get("stepstone_jobs", "https://www.stepstone.de/jobs/projektmanager-photovoltaik?ag=age_1")
time.sleep(3)
t2 = get("stepstone_work", "https://www.stepstone.de/work/projektmanager-photovoltaik?ag=age_1")
m = re.search(r'href="(/stellenangebote--[^"]+)"', t or "") or re.search(r'href="(/stellenangebote--[^"]+)"', t2 or "")
if m:
    time.sleep(3)
    get("stepstone_job", "https://www.stepstone.de" + m.group(1))

# XING: Suchseite (zwei Formen) und eine Stellenseite
time.sleep(2)
x = get("xing_search", "https://www.xing.com/jobs/search?keywords=Projektmanager%20Photovoltaik&location=Deutschland")
time.sleep(3)
x2 = get("xing_search_date", "https://www.xing.com/jobs/search?keywords=Projektmanager+Photovoltaik&sort=date")
links = re.findall(r'(https://www\.xing\.com/jobs/[a-z0-9\-]+-\d+)', (x or "") + (x2 or ""))
links += ["https://www.xing.com" + p for p in re.findall(r'href="(/jobs/[a-z0-9\-]+-\d+)"', (x or "") + (x2 or ""))]
summary["xing_links_found"] = len(set(links))
if links:
    time.sleep(3)
    get("xing_job", links[0])

# Glassdoor über JobSpy
try:
    from jobspy import scrape_jobs
    df = scrape_jobs(site_name=["glassdoor"], search_term="Projektmanager Photovoltaik", location="Deutschland",
                     country_indeed="germany", hours_old=72, results_wanted=20, fetch_description=True, verbose=1)
    rows = [] if df is None else df.to_dict("records")
    summary["glassdoor"] = {"count": len(rows),
                            "with_description": sum(1 for r in rows if isinstance(r.get("description"), str) and r.get("description")),
                            "sample": [{k: str(r.get(k))[:90] for k in ("title", "company", "location", "date_posted", "job_url")} for r in rows[:8]]}
except Exception:  # noqa: BLE001
    summary["glassdoor"] = {"error": traceback.format_exc()[-800:]}

with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=1)
print(json.dumps(summary, ensure_ascii=False, indent=1)[:5000])
