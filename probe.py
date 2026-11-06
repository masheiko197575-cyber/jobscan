"""Probe 4 (06.11.2026): JOIN-Firmenseiten (alle Stellen?), rejobs.org (richtige Adressen, Struktur),
Lever/Ashby von Munich Electrification, Status alter Stellenlinks (encosa, univativ)."""
import json, os, re, traceback
import requests

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "probe")
RAW = os.path.join(OUT, "raw4")
os.makedirs(RAW, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
      "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}
summary = {}


def get(name, url, save=True, **kw):
    try:
        r = requests.get(url, headers=kw.pop("headers", UA), timeout=30, allow_redirects=True, **kw)
        t = r.text
        info = {"url": url, "status": r.status_code, "final": r.url, "ctype": r.headers.get("content-type"), "len": len(t),
                "server": r.headers.get("server"), "cf": r.headers.get("cf-mitigated"),
                "title": ((re.search(r"<title[^>]*>(.*?)</title>", t, re.S | re.I) or [None, ""])[1] or "")[:150].strip(),
                "next_data": "__NEXT_DATA__" in t, "next_f": "self.__next_f" in t,
                "jsonld_jobposting": len(re.findall(r'"@type"\s*:\s*"JobPosting"', t)),
                "nicht_mehr": bool(re.search(r"nicht mehr verfügbar|no longer available|archiviert", t, re.I))}
        if save:
            ext = "json" if "json" in (info["ctype"] or "") else "html"
            open(os.path.join(RAW, f"{name}.{ext}"), "w", encoding="utf-8").write(t[:600000])
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', t, re.S)
            if m:
                open(os.path.join(RAW, f"{name}.next.json"), "w", encoding="utf-8").write(m.group(1))
        summary[name] = info
        return r
    except Exception as ex:  # noqa: BLE001
        summary[name] = {"url": url, "error": repr(ex)[:300]}
        return None


# --- JOIN
for slug in ("axsol", "junizenergy", "encosa615", "solarize"):
    r = get(f"join_{slug}", f"https://join.com/companies/{slug}")
    get(f"join_{slug}_p2", f"https://join.com/companies/{slug}?page=2")
    if r is not None:
        t = r.text
        ids = sorted(set(re.findall(rf'/companies/{slug}/(\d+)-', t)))
        summary[f"join_{slug}"]["job_ids_in_html"] = ids
        cid = re.findall(r'"companyId"\s*:\s*(\d+)', t)[:3] + re.findall(r'"company"\s*:\s*\{\s*"id"\s*:\s*(\d+)', t)[:3]
        summary[f"join_{slug}"]["company_ids"] = cid
        apis = sorted(set(re.findall(r'https?://[a-z0-9.-]*join\.com/api[^"\'\s<>)]*', t)))[:30]
        summary[f"join_{slug}"]["api_urls"] = apis
        summary[f"join_{slug}"]["api_paths"] = sorted(set(re.findall(r'["\'](/api/[^"\'\s<>]{3,120})["\']', t)))[:40]
        for c in sorted(set(cid))[:1]:
            for k, u in {"pub": f"https://join.com/api/public/companies/{c}/jobs?page=1&pageSize=50",
                         "pub2": f"https://join.com/api/public/companies/{c}/jobs?locale=de-DE&page=1&pageSize=50",
                         "jobs": f"https://join.com/api/jobs?companyId={c}&page=1&pageSize=50"}.items():
                get(f"join_{slug}_api_{k}", u, headers={**UA, "Accept": "application/json"})
get("join_encosa_old", "https://join.com/companies/encosa615/15495404-operations-procurement-manager-bess-m-w-d")
get("join_univativ_old", "https://join.com/companies/univativ6/14203537-technischer-sachbearbeiter-m-w-d-netzbetrieb")

# --- Munich Electrification (Lever / Ashby)
get("lever_me", "https://api.lever.co/v0/postings/MunichElectrification?mode=json")
get("lever_me_old", "https://jobs.lever.co/MunichElectrification/f6b92668-2ee5-419c-b32d-59e3deae39df")
get("ashby_me", "https://api.ashbyhq.com/posting-api/job-board/munich-electrification")

# --- rejobs.org
for name, u in {"rj_pm": "https://rejobs.org/de/erneuerbare-energien-jobs/projektmanagement",
                "rj_pm_p2": "https://rejobs.org/de/erneuerbare-energien-jobs/projektmanagement?page=2",
                "rj_de_tag": "https://rejobs.org/de/erneuerbare-energien-jobs/deutschland?tag=projektmanagement",
                "rj_de": "https://rejobs.org/de/erneuerbare-energien-jobs/deutschland",
                "rj_de_pm": "https://rejobs.org/de/erneuerbare-energien-jobs/deutschland/projektmanagement",
                "rj_batt": "https://rejobs.org/de/erneuerbare-energien-jobs/batterietechnologie",
                "rj_job": "https://rejobs.org/de/erneuerbare-energien-jobs/141582-sales-manager-mwd-fur-batteriespeichersysteme-axsol-gmbh",
                "rj_robots": "https://rejobs.org/robots.txt",
                "rj_sitemap": "https://rejobs.org/sitemap.xml",
                "rj_rss": "https://rejobs.org/rss",
                "rj_feed": "https://rejobs.org/feed"}.items():
    r = get(name, u)
    if r is not None and name in ("rj_pm", "rj_batt", "rj_de"):
        t = r.text
        summary[name]["job_links"] = sorted(set(re.findall(r'href="(/de/erneuerbare-energien-jobs/\d+-[^"]+)"', t)))[:60]
        summary[name]["cat_links"] = sorted(set(re.findall(r'href="(/de/erneuerbare-energien-jobs/[a-z][^"?#]*(?:\?[^"#]*)?)"', t)))[:120]
        summary[name]["api_paths"] = sorted(set(re.findall(r'["\'](/api/[^"\'\s<>]{3,120})["\']', t)))[:40]
        summary[name]["abgelaufen"] = len(re.findall(r"Abgelaufen", t))

json.dump(summary, open(os.path.join(OUT, "summary4.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk in ("status", "len", "title", "error")} for k, v in summary.items()}, ensure_ascii=False, indent=1))
