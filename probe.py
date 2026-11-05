"""Test 2: Glassdoor über JobSpy (normale Nutzung), mit Logausgabe — warum 0 Treffer?"""
import io, json, logging, os, traceback

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "probe")
os.makedirs(OUT, exist_ok=True)
summary = {}
buf = io.StringIO()
h = logging.StreamHandler(buf)
logging.getLogger().addHandler(h)
logging.getLogger().setLevel(logging.INFO)
from jobspy import scrape_jobs
for loc in ("Germany", "Deutschland", "Berlin"):
    try:
        df = scrape_jobs(site_name=["glassdoor"], search_term="Projektmanager Photovoltaik", location=loc,
                         country_indeed="germany", results_wanted=15, verbose=2)
        rows = [] if df is None else df.to_dict("records")
        summary[f"glassdoor_{loc}"] = {"count": len(rows), "sample": [{k: str(r.get(k))[:80] for k in ("title", "company", "location", "date_posted")} for r in rows[:5]]}
    except Exception:  # noqa: BLE001
        summary[f"glassdoor_{loc}"] = {"error": traceback.format_exc()[-500:]}
summary["log"] = buf.getvalue()[-4000:]
json.dump(summary, open(os.path.join(OUT, "summary2.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(json.dumps(summary, ensure_ascii=False, indent=1)[:6000])
