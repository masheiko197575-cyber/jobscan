#!/usr/bin/env python3
"""Probe 4 (15.11.2026): offene Fragen aus Probe 3 — Stellensysteme einzelner Firmen genauer ansehen.
Ergebnis: data/probe/p4.json"""
import html
import json
import os
import re
import time
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests

import scan

OUT = os.path.join(scan.DATA, "probe")
os.makedirs(OUT, exist_ok=True)
UA = {**scan.UA, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}
RES = {}
_rb = {}


def robots_ok(url):
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _rb:
        val = None
        try:
            r = requests.get(base + "/robots.txt", headers=UA, timeout=15)
            if r.status_code in (401, 403):
                val = "forbidden"
            elif r.status_code == 200 and "<html" not in r.text[:300].lower():
                rp = robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
                val = rp
        except Exception:  # noqa: BLE001
            val = None
        _rb[base] = val
    v = _rb[base]
    if v is None:
        return True
    if v == "forbidden":
        return False
    return v.can_fetch("jobscan", url)


def get(url, method="GET", body=None, timeout=25):
    if not robots_ok(url):
        return {"robots": False}
    try:
        time.sleep(1.0)
        if method == "POST":
            r = requests.post(url, json=body, headers={**UA, "Content-Type": "application/json", "Accept": "application/json"}, timeout=timeout)
        else:
            r = requests.get(url, headers=UA, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"err": f"{type(e).__name__}: {e}"[:600]}
    out = {"status": r.status_code, "final": r.url, "ctype": r.headers.get("content-type", "")[:50]}
    if r.status_code == 200:
        r.encoding = r.encoding if (r.encoding and r.encoding.lower() != "iso-8859-1") else (r.apparent_encoding or "utf-8")
        out["text"] = r.text
    return out


A_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
HREF_RE = re.compile(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
GENDER = re.compile(r"\(\s*[mwdfx](?:\s*[/|*,]\s*[mwdfx]){1,3}\s*\)|all genders|\bm/w/d\b|\bw/m/d\b|\bd/m/w\b|\(gn\)", re.I)


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def anchors(base, text, flt=None, n=40):
    out = []
    for m in A_RE.finditer(text):
        h = HREF_RE.search(m.group(1))
        if not h:
            continue
        href = html.unescape(next(g for g in h.groups() if g is not None)).strip()
        u = urljoin(base, href)
        t = clean(m.group(2))
        if flt and not re.search(flt, u + " " + t, re.I):
            continue
        out.append([t[:90], u[-140:]])
        if len(out) >= n:
            break
    return out


def page_summary(url, flt=None):
    r = get(url)
    t = r.pop("text", None)
    if not t:
        return r
    heads = [clean(x)[:100] for x in re.findall(r"<h[1-5][^>]*>(.*?)</h[1-5]>", t, re.S | re.I)]
    r.update({
        "len": len(t),
        "title": clean((re.search(r"<title[^>]*>(.*?)</title>", t, re.S | re.I) or [None, ""])[1])[:90],
        "gender_hits": len(GENDER.findall(clean(t))),
        "gender_heads": [h for h in heads if GENDER.search(h)][:25],
        "heads": heads[:30],
        "iframes": re.findall(r"<iframe[^>]+src=[\"']([^\"']+)", t, re.I)[:6],
        "script_hosts": sorted({urlparse(urljoin(url, s)).netloc for s in re.findall(r"<script[^>]+src=[\"']([^\"']+)", t, re.I)})[:20],
        "ld_jobposting": t.count('"JobPosting"'),
        "anchors": anchors(url, t, flt, 40),
        "gender_snips": [clean(t[max(0, m.start() - 160):m.end() + 20])[-200:] for m in list(GENDER.finditer(t))[:12]],
    })
    return r


def snippet(text, pat, n=6, w=260):
    return [text[max(0, m.start() - w):m.end() + w] for m in list(re.finditer(pat, text or "", re.I))[:n]]


def safe(name, fn):
    try:
        RES[name] = fn()
    except Exception as e:  # noqa: BLE001
        RES[name] = {"crash": f"{type(e).__name__}: {e}"[:400]}


# a) d.vinci-Host bei Encavis und Stauferwerk
def dvinci_hosts():
    out = {}
    for name, url in (("encavis", "https://www.encavis.com/de/karriere/stellenanzeigen"), ("stauferwerk", "https://www.stauferwerk.de/karriere")):
        r = get(url)
        t = r.get("text") or ""
        out[name] = {"status": r.get("status"), "hosts": sorted(set(re.findall(r"[a-z0-9-]+\.dvinci-hr\.com", t, re.I)))[:5],
                     "snips": [s[:520] for s in snippet(t, r"dvinci", 6, 220)]}
    return out


# b) Autargreen API
def autargreen():
    r = get("https://hub.autargreen.de/api/public/jobs")
    t = r.pop("text", None)
    if t:
        try:
            d = json.loads(t)
            r["json"] = json.dumps(d, ensure_ascii=False)[:2500]
        except Exception:  # noqa: BLE001
            r["head"] = t[:800]
    return r


# c) Workday: Facetten (GETEC, Generac/Pramac)
def workday():
    out = {}
    for name, host, tenant, site in (("getec", "getec.wd103.myworkdayjobs.com", "getec", "External_Careers"),
                                     ("generac", "generac.wd5.myworkdayjobs.com", "generac", "ExternalCareers")):
        url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        r = get(url, "POST", {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""})
        t = r.pop("text", None)
        if not t:
            out[name] = r
            continue
        d = json.loads(t)
        facets = []
        for f in d.get("facets") or []:
            vals = f.get("values") or []
            sub = []
            for v in vals:
                if v.get("values"):  # verschachtelt
                    for vv in v["values"]:
                        sub.append([v.get("descriptor"), vv.get("descriptor"), vv.get("id"), vv.get("count")])
                else:
                    sub.append([v.get("descriptor"), v.get("id"), v.get("count")])
            facets.append({"param": f.get("facetParameter"), "descriptor": f.get("descriptor"),
                           "values": [x for x in sub if re.search(r"german|deutsch|fellbach|pfullingen", json.dumps(x), re.I)][:10],
                           "n": len(sub)})
        out[name] = {"total": d.get("total"), "facets": facets,
                     "sample": [[p.get("title"), p.get("locationsText"), p.get("externalPath")] for p in (d.get("jobPostings") or [])[:6]]}
        # mit Deutschland-Facette
        for f in facets:
            for v in f["values"]:
                fid = v[-2] if len(v) == 4 else v[1]
                if fid and re.search(r"germany|deutschland", json.dumps(v), re.I):
                    r2 = get(url, "POST", {"appliedFacets": {f["param"]: [fid]}, "limit": 20, "offset": 0, "searchText": ""})
                    t2 = r2.pop("text", None)
                    if t2:
                        d2 = json.loads(t2)
                        out[name].setdefault("germany", []).append({"param": f["param"], "id": fid, "total": d2.get("total"),
                                                                    "sample": [[p.get("title"), p.get("locationsText")] for p in (d2.get("jobPostings") or [])[:12]]})
    return out


# d) TenneT (Avature): 50 je Seite, RSS?
def tennet():
    out = {}
    for off in (0, 50):
        url = f"https://careers.tennet.eu/de_DE/careers/SearchJobs/?jobRecordsPerPage=50&jobOffset={off}"
        r = get(url)
        t = r.pop("text", None) or ""
        links = sorted(set(re.findall(r"JobDetail\?[^\"'<>\s]*jobId=(\d+)", t)))
        out[off] = {"status": r.get("status"), "jobs": len(links), "total_hint": snippet(t, r"(?:von|of)\s+\d+\s+(?:Ergebnissen|results|Stellen)", 2, 60),
                    "rss": sorted(set(re.findall(r"[^\"'\s]*(?:feed|rss)[^\"'\s]*", t, re.I)))[:6],
                    "titles": anchors(url, t, r"JobDetail", 8)}
    return out


# e) RES (Jobvite): Seiten mit Deutschland
def res():
    out = {}
    for url in ("https://jobs.jobvite.com/resgroup/search?l=Germany&p=0", "https://jobs.jobvite.com/resgroup/search?q=&l=Germany",
                "https://jobs.jobvite.com/resgroup/jobs/positions?p=1", "https://jobs.jobvite.com/resgroup/search?l=V%C3%B6rstetten",
                "https://jobs.jobvite.com/resgroup/jobs/positions?l=Germany"):
        r = get(url)
        t = r.pop("text", None) or ""
        a = anchors(url, t, r"/resgroup/job/", 400)
        out[url] = {"status": r.get("status"), "jobs": len(a), "germany": [x for x in a if re.search(r"germany|deutschland", x[0], re.I)][:30],
                    "pager": sorted(set(re.findall(r"[?&]p=(\d+)", t)))[:12], "count_txt": snippet(t, r"\d+\s+(?:jobs|results|Stellen)", 2, 40)}
    return out


# f) Neoen und Metlen: JSON-LD auf Stellenseiten
def ld_pages():
    out = {}
    sm = get("https://careers.neoen.com/sitemap.xml").get("text") or ""
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sm)
    de = [u for u in locs if re.search(r"karlsruhe|hamburg|berlin|m[uü]nchen|deutschland|germany|allemagne", u, re.I)]
    out["neoen_de_urls"] = de[:10]
    for u in (de[:1] + [x for x in locs if "annonce" in x and x not in de][:1]):
        r = get(u)
        t = r.pop("text", None) or ""
        det = scan.xing_details(t) if t else None
        out[u] = {"status": r.get("status"), "ld": bool(det), "det": {k: (v[:120] if isinstance(v, str) else v) for k, v in (det or {}).items() if k != "description"},
                  "desc_len": len((det or {}).get("description") or "")}
    sm2 = get("https://careers.metlen.com/sitemap.xml").get("text") or ""
    de2 = [u for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sm2) if "Germany" in u]
    out["metlen_de_urls"] = de2[:12]
    for u in de2[:1]:
        r = get(u)
        t = r.pop("text", None) or ""
        det = scan.xing_details(t) if t else None
        out[u] = {"status": r.get("status"), "ld": bool(det), "det": {k: (v[:120] if isinstance(v, str) else v) for k, v in (det or {}).items() if k != "description"},
                  "desc_len": len((det or {}).get("description") or "")}
    return out


# g) Seiten ohne erkannte Stellenliste
def misc_pages():
    out = {}
    for name, url, flt in (
        ("kodiak", "https://kodiak.eu/jobs/open-jobs/", None),
        ("prokon", "https://www.prokon.energy/de-de/karriere/", None),
        ("anumar", "https://anumar.de/karriere/", None),
        ("bw-ess", "https://bw-ess.com/career", None),
        ("nofar", "https://www.nofar-energy.com/careers", None),
        ("econergy", "https://www.econergytech.com/careers/", r"job|career"),
        ("uhl", "https://www.uhl-windkraft.de/verstaerkung-gesucht-wir-stellen-ein/", None),
        ("mein-solar", "https://www.mein-solar.com/Karriere", None),
        ("edf", "https://www.edf-powersolutions.de/karriere", None),
        ("energiekontor", "https://www.energiekontor.de/karriere.html", r"karriere|stellen|\.pdf"),
        ("qcells-de", "https://www.q-cells.de/1/ueber-uns/jobs", r"jobs|page|seite"),
        ("ibvogt", "https://www.ibvogt.com/career/", None),
        ("transnetbw", "https://www.transnetbw.de/de/karriere/stellenangebote", r"stellen|job"),
        ("kostal", "https://www.kostal.com/de-de/karriere", None),
        ("mounting", "https://www.mounting-systems.com/de/karriere/", None),
        ("orange-solar", "https://www.orange-solar.de/karriere", None),
        ("netze-odr", "https://www.netze-odr.de/karriere/jobmarkt", r"careers\.enbw"),
        ("e3dc", "https://www.e3dc.com/unternehmen/karriere/", r"hager"),
    ):
        safe_out = page_summary(url, flt)
        out[name] = safe_out
    return out


# h) TLS-Fehler im Wortlaut
def tls():
    out = {}
    for u in ("https://www.numbat.energy/", "https://www.ostwind.de/", "https://www.smart-power.net/", "https://www.stadtwerke-backnang.de/",
              "https://www.sunvigo.de/", "https://www.pp-energiesysteme.de/"):
        try:
            requests.get(u, headers=UA, timeout=20)
            out[u] = "ok"
        except Exception as e:  # noqa: BLE001
            out[u] = f"{type(e).__name__}: {e}"[-500:]
    return out


# i) EnBW-Sitemap enthält Netze-ODR-Stellen?
def enbw_odr():
    t = get("https://careers.enbw.com/sitemap.xml").get("text") or ""
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", t)
    subs = []
    if "<sitemapindex" in t:
        for sm in locs[:10]:
            subs += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", get(html.unescape(sm)).get("text") or "")
        locs = subs
    keys = ["EBQEBQGLOBAL25279", "EBQEBQGLOBAL25333", "EBQEBQGLOBAL25550", "EBQEBQGLOBAL25724", "BAL24196"]
    return {"locs": len(locs), "found": {k: any(k in u for u in locs) for k in keys}}


# j) d.vinci- und Workable-Datensätze im Aufbau
def structures():
    out = {}
    r = get("https://scharr.dvinci-hr.com/jobPublication/list.json")
    t = r.pop("text", None)
    if t:
        d = json.loads(t)
        it = d[0] if d else {}
        out["dvinci"] = {k: (json.dumps(v, ensure_ascii=False)[:260]) for k, v in it.items()}
    r = get("https://apply.workable.com/api/v1/widget/accounts/enfinity-global-2")
    t = r.pop("text", None)
    if t:
        d = json.loads(t)
        jobs = d.get("jobs") or []
        out["workable"] = {"first": jobs[0] if jobs else None, "countries": sorted({str(j.get("country")) for j in jobs}),
                           "de": [[j.get("title"), j.get("city"), j.get("country")] for j in jobs if re.search(r"germany|deutschland", json.dumps(j), re.I)][:10]}
        if jobs:
            sc = jobs[0].get("shortcode")
            r2 = get(f"https://apply.workable.com/api/v2/accounts/enfinity-global-2/jobs/{sc}")
            t2 = r2.pop("text", None)
            out["workable_detail"] = {"status": r2.get("status"), "keys": sorted(json.loads(t2).keys()) if t2 else None,
                                      "robots": r2.get("robots")}
    return out


for name, fn in (("dvinci_hosts", dvinci_hosts), ("autargreen", autargreen), ("workday", workday), ("tennet", tennet), ("res", res),
                 ("ld_pages", ld_pages), ("misc", misc_pages), ("tls", tls), ("enbw_odr", enbw_odr), ("structures", structures)):
    t0 = time.time()
    safe(name, fn)
    print(name, round(time.time() - t0), "s", flush=True)

json.dump(RES, open(os.path.join(OUT, "p4.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("fertig")
