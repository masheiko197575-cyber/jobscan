#!/usr/bin/env python3
"""Täglicher Vakanzen-Scan für die Jobsuche-Tabelle.

Quellen:
  * Arbeitsagentur Jobsuche-API (dokumentiert von bundesAPI/jobsuche-api, öffentlicher Client-Key)
    - Branchen-Suchwörter × (Deutschland | 75 km um Ludwigsburg | Homeoffice), Fenster N Tage
    - Arbeitgeber aus dem Beobachtungs-Verzeichnis (Suche nach Name)
  * Personio-XML-Feeds, Recruitee-, SmartRecruiters- und Workday-APIs beobachteter Firmen
  * Atom-Feeds von eejobs.de und greenjobs.de
  * optional python-jobspy (Indeed, LinkedIn-Gastsuche) — fällt still aus, wenn blockiert

Ergebnis: data/today.json (+ data/seen.json, data/details.json, data/run-log.json).
Keine Zugangsdaten, keine personenbezogenen Daten.
"""
import base64
import datetime as dt
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
os.makedirs(DATA, exist_ok=True)

CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))

API = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4"
AA_HEADERS = {"X-API-Key": "jobboerse-jobsuche", "User-Agent": "jobscan/1.0 (+github)"}
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) jobscan/1.0"}

NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date().isoformat()
START = time.time()

ERRORS = []
STATS = {}
EXCL = re.compile(CFG["title_exclude"], re.I)
PREF = re.compile(CFG["title_prefer"], re.I)
INDUSTRY = re.compile(CFG["industry_regex"], re.I)
MAX_DESC = int(CFG.get("max_description_chars", 4000))


def log(*a):
    print(f"[{int(time.time() - START):4d}s]", *a, flush=True)


def err(msg):
    ERRORS.append(str(msg)[:300])
    log("ERROR", str(msg)[:300])


def load_json(name, default):
    p = os.path.join(DATA, name)
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            err(f"{name} unlesbar: {e}")
    return default


def save_json(name, obj):
    p = os.path.join(DATA, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)


def strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<\s*(br|/p|/li|/div|/h\d|/tr)\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def cut(s, n=MAX_DESC):
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def title_tier(title):
    """'drop' | 'prefer' | 'other'"""
    t = title or ""
    if EXCL.search(t):
        return "drop"
    if PREF.search(t):
        return "prefer"
    return "other"


# ----------------------------------------------------------------------------- HTTP

def http_get(url, params=None, headers=None, tries=3, as_json=True, timeout=40):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers or UA, timeout=timeout)
            if r.status_code == 429:
                time.sleep(15 * (i + 1))
                last = "429"
                continue
            if r.status_code in (404, 410):
                return None
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(4)
                continue
            r.raise_for_status()
            return r.json() if as_json else r.text
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3)
    err(f"GET {url} {params or ''}: {last}")
    return None


def http_post_json(url, body, tries=2, timeout=40):
    last = None
    h = dict(UA)
    h.update({"Content-Type": "application/json", "Accept": "application/json"})
    for _ in range(tries):
        try:
            r = requests.post(url, json=body, headers=h, timeout=timeout)
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(4)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3)
    err(f"POST {url}: {last}")
    return None


# ----------------------------------------------------------------------------- Arbeitsagentur

def aa_search(was, days, extra=None, max_pages=12, size=100):
    out = []
    page = 1
    total = None
    while page <= max_pages:
        params = {"was": was, "angebotsart": 1, "veroeffentlichtseit": days, "size": size, "page": page}
        if extra:
            params.update(extra)
        d = http_get(f"{API}/jobs", params, AA_HEADERS)
        if not d:
            break
        items = d.get("stellenangebote") or []
        if total is None:
            total = d.get("maxErgebnisse")
        if not items:
            break
        out.extend(items)
        if total is not None and len(out) >= int(total):
            break
        if len(items) < 10:
            break
        page += 1
        time.sleep(0.6)
    return out, total


def aa_norm(it, found_by):
    ort = it.get("arbeitsort") or {}
    loc = ", ".join(x for x in [ort.get("plz"), ort.get("ort")] if x)
    if ort.get("region") and ort.get("region") not in loc:
        loc = f"{loc} ({ort.get('region')})" if loc else ort.get("region")
    km = ort.get("entfernung")
    try:
        km = round(float(km)) if km not in (None, "") else None
    except Exception:  # noqa: BLE001
        km = None
    refnr = it.get("refnr") or ""
    return {
        "id": refnr,
        "refnr": refnr,
        "source": "Jobbörse",
        "title": (it.get("titel") or it.get("beruf") or "").strip(),
        "beruf": it.get("beruf"),
        "company": (it.get("arbeitgeber") or "").strip(),
        "location": loc,
        "km": km,
        "url": f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}" if refnr else None,
        "published": (it.get("aktuelleVeroeffentlichungsdatum") or "")[:10] or None,
        "modified": it.get("modifikationsTimestamp"),
        "start": it.get("eintrittsdatum"),
        "found_by": [found_by],
        "homeoffice": False,
    }


def aa_details(refnr):
    enc = base64.b64encode(refnr.encode("utf-8")).decode("ascii")
    d = http_get(f"{API}/jobdetails/{enc}", headers=AA_HEADERS)
    if not d:
        return None
    desc = strip_html(d.get("stellenbeschreibung") or "")
    orte = d.get("arbeitsorte") or []
    orte_txt = "; ".join(
        ", ".join(x for x in [o.get("plz"), o.get("ort"), o.get("region")] if x) for o in orte if isinstance(o, dict)
    )
    azm = d.get("arbeitszeitmodelle")
    info = {
        "description": cut(desc),
        "worktime": "; ".join(azm) if isinstance(azm, list) else azm,
        "fixed_term": d.get("befristung"),
        "salary": d.get("verguetung") or d.get("vergütung"),
        "locations": orte_txt or None,
        "external_url": d.get("externeUrl") or d.get("allianzpartnerUrl"),
        "employer_profile": (d.get("arbeitgeberdarstellung") and cut(strip_html(d.get("arbeitgeberdarstellung")), 800)) or None,
        "published": (d.get("aktuelleVeroeffentlichungsdatum") or "")[:10] or None,
        "first_published": (d.get("ersteVeroeffentlichungsdatum") or "")[:10] or None,
        "zeitarbeit": d.get("istZeitarbeit"),
        "homeoffice_hint": bool(azm and any("HEIM" in str(a).upper() or "TELE" in str(a).upper() for a in azm)),
    }
    return info


def scan_jobboerse(details_cache):
    jobs = {}
    days = int(CFG.get("days", 7))
    terms = CFG["industry_terms"]
    geo = CFG["geo"]
    per_query = {}

    def add(items, found_by, flag=None):
        n = 0
        for it in items:
            rec = aa_norm(it, found_by)
            if not rec["refnr"]:
                continue
            cur = jobs.get(rec["refnr"])
            if cur:
                if found_by not in cur["found_by"]:
                    cur["found_by"].append(found_by)
                if rec["km"] is not None and cur.get("km") is None:
                    cur["km"] = rec["km"]
            else:
                jobs[rec["refnr"]] = rec
                cur = rec
                n += 1
            if flag == "homeoffice":
                cur["homeoffice"] = True
        return n

    for t in terms:
        items, total = aa_search(t, days, {"wo": geo["near"]["wo"], "umkreis": geo["near"]["umkreis"]})
        per_query[f"рядом: {t}"] = total if total is not None else len(items)
        add(items, f"Jobbörse рядом: {t}")
        time.sleep(0.4)
        items, total = aa_search(t, days, {"arbeitszeit": "ho"})
        per_query[f"удалённо: {t}"] = total if total is not None else len(items)
        add(items, f"Jobbörse удалённо: {t}", "homeoffice")
        time.sleep(0.4)
        items, total = aa_search(t, days)
        per_query[f"DE: {t}"] = total if total is not None else len(items)
        add(items, f"Jobbörse DE: {t}")
        time.sleep(0.4)
        log(f"Jobbörse «{t}»: insgesamt {len(jobs)} eindeutige")

    STATS["jobboerse_per_query"] = per_query
    STATS["jobboerse_unique"] = len(jobs)

    # Tier nach Titel
    tiers = {"prefer": [], "other": [], "drop": 0}
    for rec in jobs.values():
        tier = title_tier(rec["title"])
        rec["title_tier"] = tier
        if tier == "drop":
            tiers["drop"] += 1
        else:
            tiers[tier].append(rec)
    STATS["jobboerse_tiers"] = {"prefer": len(tiers["prefer"]), "other": len(tiers["other"]), "drop": tiers["drop"]}
    log(f"Jobbörse: prefer {len(tiers['prefer'])}, other {len(tiers['other'])}, drop {tiers['drop']}")

    # Beschreibungen für 'prefer' (Cache)
    max_details = int(CFG.get("max_details", 250))
    fetched = 0
    for rec in sorted(tiers["prefer"], key=lambda r: (r.get("km") is None, r.get("km") or 0, r.get("published") or "")):
        c = details_cache.get(rec["refnr"])
        if c is None and fetched < max_details:
            c = aa_details(rec["refnr"])
            fetched += 1
            time.sleep(0.5)
            if c:
                c["cached_at"] = TODAY
                details_cache[rec["refnr"]] = c
        if c:
            rec.update({k: v for k, v in c.items() if k != "cached_at"})
            if c.get("homeoffice_hint"):
                rec["homeoffice"] = True
        else:
            rec["description"] = None
    STATS["jobboerse_details_fetched"] = fetched
    return tiers["prefer"], tiers["other"], jobs


def scan_employers(days, details_cache):
    """Beobachtete Arbeitgeber über die Jobbörse (Suche nach Name)."""
    out = {}
    for e in CFG.get("employers", []):
        q = e.get("query") or e["name"]
        match = (e.get("match") or q).lower()
        items, total = aa_search(q, days, max_pages=4)
        hits = []
        for it in items:
            emp = (it.get("arbeitgeber") or "").lower()
            if match in emp:
                rec = aa_norm(it, f"Компании под наблюдением: {e['name']}")
                rec["title_tier"] = title_tier(rec["title"])
                if rec["title_tier"] == "prefer":
                    c = details_cache.get(rec["refnr"])
                    if c is None:
                        c = aa_details(rec["refnr"])
                        time.sleep(0.5)
                        if c:
                            c["cached_at"] = TODAY
                            details_cache[rec["refnr"]] = c
                    if c:
                        rec.update({k: v for k, v in c.items() if k != "cached_at"})
                hits.append(rec)
        out[e["id"]] = {"name": e["name"], "method": "jobboerse", "query": q, "total_hits": total, "jobs": hits}
        time.sleep(0.5)
    log(f"Arbeitgeber-Suche: {len(out)} Firmen")
    return out


# ----------------------------------------------------------------------------- Personio / Recruitee / SmartRecruiters / Workday

def scan_personio(e):
    xml = http_get(e["feed"], headers=UA, as_json=False)
    if xml is None:
        return None
    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except Exception as ex:  # noqa: BLE001
        err(f"Personio {e['name']}: XML {ex}")
        return None
    base = e["feed"].split("/xml")[0]
    jobs = []
    for pos in root.iter("position"):
        g = lambda tag: (pos.findtext(tag) or "").strip()  # noqa: E731
        pid = g("id")
        title = g("name")
        descs = []
        for jd in pos.iter("jobDescription"):
            nm = (jd.findtext("name") or "").strip()
            val = strip_html(jd.findtext("value") or "")
            if val:
                descs.append(f"{nm}: {val}" if nm else val)
        rec = {
            "id": f"personio-{pid}",
            "source": "Сайт компании",
            "company": e["name"],
            "title": title,
            "location": g("office"),
            "department": g("department"),
            "employment": g("employmentType"),
            "schedule": g("schedule"),
            "seniority": g("seniority"),
            "published": (g("createdAt") or "")[:10] or None,
            "url": f"{base}/job/{pid}" if pid else base,
            "title_tier": title_tier(title),
            "found_by": [f"Компании под наблюдением: {e['name']}"],
        }
        if rec["title_tier"] == "prefer":
            rec["description"] = cut("\n".join(descs))
        jobs.append(rec)
    return jobs


def scan_recruitee(e):
    d = http_get(e["api"], headers=UA)
    if not d:
        return None
    jobs = []
    for o in d.get("offers", []):
        title = o.get("title") or ""
        rec = {
            "id": f"recruitee-{o.get('id')}",
            "source": "Сайт компании",
            "company": e["name"],
            "title": title,
            "location": ", ".join(x for x in [o.get("city"), o.get("country")] if x),
            "employment": o.get("employment_type_code"),
            "remote": o.get("remote"),
            "published": (o.get("published_at") or "")[:10] or None,
            "url": o.get("careers_url"),
            "title_tier": title_tier(title),
            "found_by": [f"Компании под наблюдением: {e['name']}"],
        }
        if rec["title_tier"] == "prefer":
            rec["description"] = cut(strip_html((o.get("description") or "") + "\n" + (o.get("requirements") or "")))
        jobs.append(rec)
    return jobs


def scan_smartrecruiters(e):
    base = f"https://api.smartrecruiters.com/v1/companies/{e['company']}/postings"
    d = http_get(base, {"limit": 100}, headers=UA)
    if not d:
        return None
    jobs = []
    for p in d.get("content", []):
        loc = p.get("location") or {}
        country = (loc.get("country") or "").lower()
        if e.get("country") and country and country != e["country"].lower():
            continue
        title = p.get("name") or ""
        rec = {
            "id": f"smartrecruiters-{p.get('id')}",
            "source": "Сайт компании",
            "company": e["name"],
            "title": title,
            "location": ", ".join(x for x in [loc.get("city"), loc.get("country")] if x),
            "remote": loc.get("remote"),
            "published": (p.get("releasedDate") or "")[:10] or None,
            "url": f"https://jobs.smartrecruiters.com/{e['company']}/{p.get('id')}",
            "title_tier": title_tier(title),
            "found_by": [f"Компании под наблюдением: {e['name']}"],
        }
        if rec["title_tier"] == "prefer":
            det = http_get(f"{base}/{p.get('id')}", headers=UA)
            if det:
                secs = ((det.get("jobAd") or {}).get("sections") or {})
                txt = "\n".join(strip_html((secs.get(k) or {}).get("text") or "") for k in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"))
                rec["description"] = cut(txt)
            time.sleep(0.3)
        jobs.append(rec)
    return jobs


def scan_workday(e):
    host, tenant, site = e["host"], e["tenant"], e["site"]
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    jobs = []
    offset = 0
    locf = re.compile(e["location_regex"], re.I) if e.get("location_regex") else None
    while offset < 400:
        d = http_post_json(url, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": e.get("searchText", "")})
        if not d:
            break
        posts = d.get("jobPostings") or []
        for p in posts:
            title = p.get("title") or ""
            loc = p.get("locationsText") or ""
            if locf and not locf.search(loc + " " + title):
                continue
            path = p.get("externalPath") or ""
            rec = {
                "id": f"workday-{tenant}-{(p.get('bulletFields') or [path])[0]}",
                "source": "Сайт компании",
                "company": e["name"],
                "title": title,
                "location": loc,
                "published": p.get("postedOn"),
                "url": f"https://{host}/{site}{path}",
                "title_tier": title_tier(title),
                "found_by": [f"Компании под наблюдением: {e['name']}"],
            }
            if rec["title_tier"] == "prefer":
                det = http_get(f"https://{host}/wday/cxs/{tenant}/{site}{path}", headers={**UA, "Accept": "application/json"})
                info = (det or {}).get("jobPostingInfo") or {}
                if info:
                    rec["description"] = cut(strip_html(info.get("jobDescription") or ""))
                    rec["published"] = info.get("startDate") or rec["published"]
                time.sleep(0.3)
            jobs.append(rec)
        total = d.get("total") or 0
        offset += 20
        if offset >= total or not posts:
            break
        time.sleep(0.4)
    return jobs


def scan_feeds():
    out = {}
    for kind, fn in (("personio", scan_personio), ("recruitee", scan_recruitee), ("smartrecruiters", scan_smartrecruiters), ("workday", scan_workday)):
        for e in CFG.get(kind, []):
            try:
                jobs = fn(e)
            except Exception as ex:  # noqa: BLE001
                err(f"{kind} {e.get('name')}: {ex}")
                jobs = None
            entry = out.setdefault(e["id"], {"name": e["name"], "method": kind, "jobs": []})
            if jobs is None:
                entry["error"] = (entry.get("error") or "") + f"{kind} не открылся; "
            else:
                entry["jobs"].extend(jobs)
                entry["method"] = entry["method"] if entry["method"] == kind else f"{entry['method']}+{kind}"
            time.sleep(0.3)
    log(f"Feeds: {len(out)} Firmen")
    return out


# ----------------------------------------------------------------------------- Atom-Feeds (eejobs, greenjobs)

def scan_atom():
    out = []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for e in CFG.get("atom", []):
        xml = http_get(e["url"], headers=UA, as_json=False)
        if not xml:
            continue
        try:
            root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
        except Exception as ex:  # noqa: BLE001
            err(f"Atom {e['name']}: {ex}")
            continue
        entries = root.findall("a:entry", ns) or root.findall("entry")
        n = 0
        for en in entries:
            def gx(tag):
                el = en.find(f"a:{tag}", ns)
                if el is None:
                    el = en.find(tag)
                return (el.text or "").strip() if el is not None and el.text else ""
            title = gx("title")
            link_el = en.find("a:link", ns)
            if link_el is None:
                link_el = en.find("link")
            link = (link_el.get("href") if link_el is not None else "") or gx("id")
            summary = strip_html(gx("summary") or gx("content"))
            if title_tier(title) == "drop":
                continue
            if not INDUSTRY.search(title + " " + summary):
                continue
            out.append({
                "id": f"{e['id']}-{re.sub(r'[^0-9a-zA-Z]+', '-', link)[-60:]}",
                "source": e["name"],
                "title": title,
                "company": gx("author") or None,
                "summary": cut(summary, 1200),
                "published": (gx("updated") or gx("published"))[:10] or None,
                "url": link,
                "title_tier": title_tier(title),
                "found_by": [f"{e['name']}: Atom"],
            })
            n += 1
        log(f"Atom {e['name']}: {n} relevante von {len(entries)}")
        STATS[f"atom_{e['id']}"] = {"entries": len(entries), "kept": n}
    return out


# ----------------------------------------------------------------------------- JobSpy (optional)

def scan_jobspy():
    cfg = CFG.get("jobspy") or {}
    if not cfg.get("enabled", True):
        return []
    try:
        from jobspy import scrape_jobs  # type: ignore
    except Exception as ex:  # noqa: BLE001
        err(f"jobspy nicht verfügbar: {ex}")
        return []
    out = {}
    for term in cfg.get("terms", []):
        for site in cfg.get("sites", ["indeed", "linkedin"]):
            try:
                df = scrape_jobs(
                    site_name=[site],
                    search_term=term,
                    location=cfg.get("location", "Deutschland"),
                    country_indeed=cfg.get("country_indeed", "germany"),
                    hours_old=int(cfg.get("hours_old", 24)),
                    results_wanted=int(cfg.get("results_wanted", 40)),
                    linkedin_fetch_description=False,
                    description_format="markdown",
                    verbose=0,
                )
            except Exception as ex:  # noqa: BLE001
                err(f"jobspy {site} «{term}»: {str(ex)[:120]}")
                continue
            if df is None or len(df) == 0:
                continue
            for _, row in df.iterrows():
                url = str(row.get("job_url") or "")
                if not url or url in out:
                    if url in out and f"{site}: {term}" not in out[url]["found_by"]:
                        out[url]["found_by"].append(f"{site}: {term}")
                    continue
                title = str(row.get("title") or "")
                tier = title_tier(title)
                if tier == "drop":
                    continue
                desc = row.get("description")
                desc = "" if desc is None or (isinstance(desc, float)) else str(desc)
                dp = row.get("date_posted")
                out[url] = {
                    "id": f"{site}-{re.sub(r'[^0-9a-zA-Z]+', '-', url)[-50:]}",
                    "source": {"indeed": "Indeed", "linkedin": "LinkedIn"}.get(site, site),
                    "title": title,
                    "company": str(row.get("company") or ""),
                    "location": str(row.get("location") or ""),
                    "remote": bool(row.get("is_remote")) if row.get("is_remote") is not None else None,
                    "published": str(dp)[:10] if dp is not None and str(dp) not in ("NaT", "nan", "None") else None,
                    "url": url,
                    "title_tier": tier,
                    "description": cut(strip_html(desc)) if desc else None,
                    "found_by": [f"{site}: {term}"],
                }
            time.sleep(2)
    log(f"JobSpy: {len(out)} Treffer")
    return list(out.values())


# ----------------------------------------------------------------------------- main

def main():
    details_cache = load_json("details.json", {})
    seen = load_json("seen.json", {"refnr": {}})

    prefer, other, alljobs = scan_jobboerse(details_cache)
    watch = scan_employers(int(CFG.get("employer_days", 30)), details_cache)
    for cid, entry in scan_feeds().items():
        cur = watch.get(cid)
        if cur:
            cur["jobs"].extend(entry.get("jobs", []))
            cur["method"] = f"{cur['method']}+{entry['method']}"
            if entry.get("error"):
                cur["error"] = entry["error"]
        else:
            watch[cid] = entry
    boards = scan_atom()
    boards += scan_jobspy()

    # neu / schon gesehen (nur Information; die Tabelle gleicht selbst ab)
    for rec in prefer + other:
        rec["seen_before"] = rec["refnr"] in seen["refnr"]
        seen["refnr"][rec["refnr"]] = TODAY
    # seen.json und details.json auf 45 Tage begrenzen
    cutoff = (NOW - dt.timedelta(days=45)).date().isoformat()
    seen["refnr"] = {k: v for k, v in seen["refnr"].items() if v >= cutoff}
    details_cache = {k: v for k, v in details_cache.items() if (v.get("cached_at") or "") >= cutoff}

    out = {
        "generated": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": TODAY,
        "days": CFG.get("days", 7),
        "schema": {
            "candidates": "Jobbörse, Titel passt zur Rolle (prefer): mit description",
            "other": "Jobbörse, Titel weder ausgeschlossen noch passend: nur Titel/Firma/Ort — kurz durchsehen",
            "watch": "beobachtete Firmen: id → {name, method, jobs[]}; jobs mit title_tier prefer haben description",
            "boards": "eejobs/greenjobs (Atom) und JobSpy (Indeed/LinkedIn), nur Titel die nicht ausgeschlossen sind",
        },
        "candidates": sorted(prefer, key=lambda r: (r.get("km") is None, r.get("km") or 0, r.get("company") or "")),
        "other": [{k: r.get(k) for k in ("refnr", "title", "company", "location", "km", "published", "url", "homeoffice", "found_by")} for r in sorted(other, key=lambda r: (r.get("km") is None, r.get("km") or 0))],
        "watch": watch,
        "boards": boards,
        "stats": STATS,
        "errors": ERRORS,
        "minutes": round((time.time() - START) / 60, 1),
    }
    save_json("today.json", out)
    save_json("seen.json", seen)
    save_json("details.json", details_cache)
    save_json("run-log.json", {"date": TODAY, "generated": out["generated"], "minutes": out["minutes"], "stats": STATS, "errors": ERRORS,
                               "counts": {"candidates": len(prefer), "other": len(other), "watch": sum(len(v.get("jobs", [])) for v in watch.values()), "boards": len(boards)}})
    log("fertig:", json.dumps({"candidates": len(prefer), "other": len(other), "watch": len(watch), "boards": len(boards), "errors": len(ERRORS)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:  # noqa: BLE001
        err(f"Abbruch: {ex}")
        save_json("run-log.json", {"date": TODAY, "aborted": True, "errors": ERRORS, "stats": STATS})
        sys.exit(1)
