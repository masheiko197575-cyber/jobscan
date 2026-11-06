#!/usr/bin/env python3
"""Täglicher Vakanzen-Scan für die Jobsuche-Tabelle.

Quellen:
  * Arbeitsagentur Jobsuche-API (dokumentiert von bundesAPI/jobsuche-api, öffentlicher Client-Key)
    - Branchen-Suchwörter × (Deutschland | 75 km um Ludwigsburg | Homeoffice), Fenster N Tage
    - Arbeitgeber aus dem Beobachtungs-Verzeichnis (Suche nach Name)
  * Personio-XML-Feeds, Recruitee-, SmartRecruiters-, Workday-, JOIN- und Ashby-APIs beobachteter Firmen
  * Atom-Feeds von eejobs.de und greenjobs.de
  * optional python-jobspy (Indeed, LinkedIn-Gastsuche) — fällt still aus, wenn blockiert

Ergebnis: data/today.json (+ data/seen.json, data/details.json, data/run-log.json).
Keine Zugangsdaten, keine personenbezogenen Daten.
"""
import base64
import hashlib
import datetime as dt
import html
import json
import os
import random
import re
import sys
import time
import xml.etree.ElementTree as ET

import requests
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
os.makedirs(DATA, exist_ok=True)

CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))

API = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc"  # Suche: v6, Details: v4 (v4-Suche liefert seit 2026 nur 403)
AA_HEADERS = {"X-API-Key": "jobboerse-jobsuche", "Accept": "application/json",
              "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
HOME = (48.8975, 9.1916)  # Ludwigsburg
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) jobscan/1.0"}

NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date().isoformat()
START = time.time()

ERRORS = []
STATS = {}
EXCL = re.compile(CFG["title_exclude"], re.I)
PREF = re.compile(CFG["title_prefer"], re.I)
INDUSTRY = re.compile(CFG["industry_regex"], re.I)
INDUSTRY_STRICT = re.compile(CFG.get("industry_strict_regex") or CFG["industry_regex"], re.I)
ADJACENT = re.compile(CFG["title_adjacent"], re.I) if CFG.get("title_adjacent") else None
CORE = re.compile(CFG["title_core"], re.I) if CFG.get("title_core") else None
MAX_DESC = int(CFG.get("max_description_chars", 4000))


SECRETS = []  # Werte, die nie in Log oder Dateien landen dürfen (API-Schlüssel)


def redact(s):
    s = str(s)
    for sec in SECRETS:
        if sec:
            s = s.replace(sec, "***")
    return s


def log(*a):
    print(f"[{int(time.time() - START):4d}s]", *(redact(x) for x in a), flush=True)


def err(msg):
    msg = redact(msg)[:300]
    ERRORS.append(msg)
    log("ERROR", msg)


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
    # Nur Wind/Wärme/Laden/Netzbau im Titel ohne PV- oder Speicherwort: raus (PV oder BESS muss zur Rolle gehören)
    if ADJACENT and ADJACENT.search(t) and not (CORE and CORE.search(t)):
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
            if r.status_code in (401, 403):
                last = f"HTTP {r.status_code} {r.text[:80]}"
                break
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(4)
                continue
            r.raise_for_status()
            return r.json() if as_json else r.content
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
        d = http_get(f"{API}/v6/jobs", params, AA_HEADERS)
        if not d:
            break
        items = d.get("ergebnisliste") or d.get("stellenangebote") or []
        if total is None:
            total = d.get("maxErgebnisse")
            if "sample_item_keys" not in STATS and items and isinstance(items[0], dict):
                STATS["sample_item_keys"] = sorted(items[0].keys())
        items = [it for it in items if isinstance(it, dict)]
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


def _km(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
    except Exception:  # noqa: BLE001
        return None
    import math
    p = math.pi / 180
    a = 0.5 - math.cos((lat - HOME[0]) * p) / 2 + math.cos(HOME[0] * p) * math.cos(lat * p) * (1 - math.cos((lon - HOME[1]) * p)) / 2
    return round(12742 * math.asin(math.sqrt(a)))


def aa_norm(it, found_by):
    # v6-Form (ergebnisliste) und alte v4-Form
    loks = it.get("stellenlokationen") or []
    lok = loks[0] if loks and isinstance(loks[0], dict) else {}
    adr = lok.get("adresse") or {}
    ort = it.get("arbeitsort") or {}
    plz = adr.get("plz") or ort.get("plz")
    stadt = adr.get("ort") or ort.get("ort")
    region = adr.get("region") or ort.get("region")
    loc = ", ".join(x for x in [plz, stadt] if x)
    if region and region not in loc:
        loc = f"{loc} ({region})" if loc else region
    km = _km(lok.get("breite"), lok.get("laenge"))
    if km is None:
        km = _km((lok.get("koordinaten") or {}).get("lat"), (lok.get("koordinaten") or {}).get("lon"))
    if km is None:
        km = it.get("entfernung") or ort.get("entfernung")
        try:
            km = round(float(km)) if km not in (None, "") else None
        except Exception:  # noqa: BLE001
            km = None
    refnr = it.get("referenznummer") or it.get("refnr") or ""
    vz = it.get("veroeffentlichungszeitraum") or {}
    published = (vz.get("von") if isinstance(vz, dict) else None) or it.get("aktuelleVeroeffentlichungsdatum") or ""
    gv, gb, fest = it.get("gehaltsspanneVon"), it.get("gehaltsspanneBis"), it.get("festgehalt")
    salary = None
    if gv or gb or fest:
        salary = f"{gv or ''}–{gb or ''}".strip("–") if (gv or gb) else str(fest)
        if it.get("verguetungsangabe"):
            salary = f"{salary} {it.get('verguetungsangabe')}"
    other_locs = [", ".join(x for x in [(l.get("adresse") or {}).get("plz"), (l.get("adresse") or {}).get("ort")] if x) for l in loks[1:] if isinstance(l, dict)]
    return {
        "id": refnr,
        "refnr": refnr,
        "source": "Jobbörse",
        "title": (it.get("stellenangebotsTitel") or it.get("titel") or it.get("hauptberuf") or it.get("beruf") or "").strip(),
        "beruf": it.get("hauptberuf") or it.get("beruf"),
        "company": (it.get("firma") or it.get("arbeitgeber") or "").strip(),
        "location": loc,
        "other_locations": [x for x in other_locs if x][:5],
        "km": km,
        "url": f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}" if refnr else None,
        "published": published[:10] or None,
        "first_published": (it.get("datumErsteVeroeffentlichung") or "")[:10] or None,
        "modified": it.get("aenderungsdatum") or it.get("modifikationsTimestamp"),
        "start": it.get("eintrittsdatum") or it.get("eintrittszeitraum"),
        "fulltime": it.get("arbeitszeitVollzeit"),
        "parttime": any(it.get(k) for k in ("arbeitszeitTeilzeit", "arbeitszeitTeilzeitFlexibel", "arbeitszeitTeilzeitVormittag", "arbeitszeitTeilzeitNachmittag", "arbeitszeitTeilzeitAbend")) or None,
        "contract": it.get("vertragsdauer"),
        "salary_listed": salary,
        "external_url": it.get("externeURL") or it.get("externeUrl"),
        "partner": it.get("allianzpartnerName"),
        "quereinstieg": it.get("quereinstiegGeeignet"),
        "minijob": it.get("istGeringfuegigeBeschaeftigung"),
        "found_by": [found_by],
        "homeoffice": bool(it.get("homeofficemoeglich")),
    }


def aa_details(refnr):
    enc = base64.b64encode(refnr.encode("utf-8")).decode("ascii")
    d = http_get(f"{API}/v4/jobdetails/{enc}", headers=AA_HEADERS)
    if not d:
        return None
    if "sample_detail_keys" not in STATS:
        STATS["sample_detail_keys"] = sorted(d.keys())[:60]
    desc = strip_html(d.get("stellenangebotsBeschreibung") or d.get("stellenbeschreibung") or "")
    orte = d.get("arbeitsorte") or d.get("stellenlokationen") or []
    parts = []
    for o in orte:
        if not isinstance(o, dict):
            continue
        a = o.get("adresse") if isinstance(o.get("adresse"), dict) else o
        parts.append(", ".join(x for x in [a.get("plz"), a.get("ort"), a.get("region")] if x))
    orte_txt = "; ".join(p for p in parts if p)
    azm = d.get("arbeitszeitmodelle")
    azm_txt = "; ".join(azm) if isinstance(azm, list) else azm
    flags = [k for k in ("arbeitszeitVollzeit", "arbeitszeitTeilzeit", "arbeitszeitSchicht", "arbeitszeitHeimarbeit", "arbeitszeitMinijob") if d.get(k)]
    info = {
        "description": cut(desc),
        "worktime": azm_txt or (", ".join(flags) if flags else None),
        "fixed_term": d.get("befristung") or d.get("vertragsdauer"),
        "salary": d.get("verguetung") or d.get("vergütung") or d.get("festgehalt") or (f"{d.get('gehaltsspanneVon') or ''}–{d.get('gehaltsspanneBis') or ''}".strip("–") if (d.get("gehaltsspanneVon") or d.get("gehaltsspanneBis")) else None),
        "locations": orte_txt or None,
        "external_url": d.get("externeUrl") or d.get("externeURL") or d.get("allianzpartnerUrl"),
        "partner": d.get("allianzpartnerName"),
        "employer_profile": (d.get("arbeitgeberdarstellung") and cut(strip_html(d.get("arbeitgeberdarstellung")), 800)) or None,
        "published": (d.get("aktuelleVeroeffentlichungsdatum") or ((d.get("veroeffentlichungszeitraum") or {}).get("von") if isinstance(d.get("veroeffentlichungszeitraum"), dict) else "") or "")[:10] or None,
        "first_published": (d.get("ersteVeroeffentlichungsdatum") or d.get("datumErsteVeroeffentlichung") or "")[:10] or None,
        "zeitarbeit": d.get("istArbeitnehmerUeberlassung") if d.get("istArbeitnehmerUeberlassung") is not None else d.get("istZeitarbeit"),
        "vermittlung": d.get("istPrivateArbeitsvermittlung"),
        "quereinstieg": d.get("quereinstiegGeeignet"),
        "homeoffice_hint": bool(d.get("homeofficemoeglich") or d.get("arbeitszeitHeimarbeit") or (azm and any("HEIM" in str(a).upper() or "TELE" in str(a).upper() for a in azm))),
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
        # v6 kennt arbeitszeit=ho nicht mehr: Homeoffice kommt aus dem Flag homeofficemoeglich der DE-Suche
        items, total = aa_search(t, days)
        per_query[f"DE: {t}"] = total if total is not None else len(items)
        add(items, f"Jobbörse DE: {t}")
        ho = [it for it in items if it.get("homeofficemoeglich")]
        per_query[f"удалённо: {t}"] = len(ho)
        add(ho, f"Jobbörse удалённо: {t}", "homeoffice")
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
            rec.update({k: v for k, v in c.items() if k != "cached_at" and v is not None})
            if c.get("homeoffice_hint"):
                rec["homeoffice"] = True
        else:
            rec["description"] = None
    # Branchen-Check: 'prefer' ohne Branchenwort in Titel+Text wandert nach 'other' (die Suchwörter treffen auch Nebensätze)
    keep, demoted = [], 0
    for rec in tiers["prefer"]:
        blob = " ".join(str(rec.get(k) or "") for k in ("title", "beruf", "company", "description", "employer_profile"))
        rec["industry_match"] = bool(INDUSTRY_STRICT.search(blob))
        if rec["industry_match"] or rec.get("description") is None:
            keep.append(rec)
        else:
            rec["title_tier"] = "other"
            rec.pop("description", None)
            tiers["other"].append(rec)
            demoted += 1
    tiers["prefer"] = keep
    STATS["jobboerse_details_fetched"] = fetched
    STATS["jobboerse_demoted_no_industry"] = demoted
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
            emp = (it.get("firma") or it.get("arbeitgeber") or "").lower()
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
                        rec.update({k: v for k, v in c.items() if k != "cached_at" and v is not None})
                hits.append(rec)
        out[e["id"]] = {"name": e["name"], "method": "jobboerse", "query": q, "total_hits": total, "jobs": hits}
        time.sleep(0.5)
    log(f"Arbeitgeber-Suche: {len(out)} Firmen")
    return out


# ----------------------------------------------------------------------------- Personio / Recruitee / SmartRecruiters / Workday

def scan_personio_json(e):
    """Ersatz, wenn die XML-Liste abgeschaltet ist (404): die Karriereseite lädt ihre Liste aus /search.json
    (ohne Datum und ohne Text — den liest die Morgenaufgabe bei Bedarf über die url)."""
    base = e["feed"].split("/xml")[0]
    d = http_get(f"{base}/search.json", headers={**UA, "Accept": "application/json"})
    if not isinstance(d, list):
        return None
    loc_re = re.compile(e["location_regex"], re.I) if e.get("location_regex") else None
    jobs = []
    for p in d:
        pid = str(p.get("id") or "")
        title = p.get("name") or ""
        office = p.get("office") or ""
        if not pid or (loc_re and not loc_re.search(f"{office} {title}")):
            continue
        rec = {
            "id": f"personio-{pid}",
            "source": "Сайт компании",
            "company": e["name"],
            "title": title,
            "location": office,
            "department": p.get("department") or None,
            "employment": p.get("employment_type"),
            "schedule": p.get("schedule"),
            "seniority": p.get("seniority"),
            "published": None,
            "url": f"{base}/job/{pid}",
            "title_tier": title_tier(title),
            "found_by": [f"Компании под наблюдением: {e['name']}"],
        }
        if rec["title_tier"] == "prefer" and strip_html(p.get("description") or ""):
            rec["description"] = cut(strip_html(p["description"]))
        jobs.append(rec)
    return jobs


def scan_personio(e):
    xml = http_get(e["feed"], headers=UA, as_json=False)
    if xml is None:
        return scan_personio_json(e)
    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except Exception as ex:  # noqa: BLE001
        err(f"Personio {e['name']}: XML {ex}")
        return None
    base = e["feed"].split("/xml")[0]
    loc_re = re.compile(e["location_regex"], re.I) if e.get("location_regex") else None
    jobs = []
    for pos in root.iter("position"):
        g = lambda tag: (pos.findtext(tag) or "").strip()  # noqa: E731
        pid = g("id")
        title = g("name")
        if loc_re and not loc_re.search(" ".join([g("office"), g("additionalOffices"), title])):
            continue
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


NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def join_page(url):
    """Daten einer JOIN-Seite (Firmen- oder Stellenseite) aus __NEXT_DATA__; None, wenn die Seite nicht lädt."""
    raw = http_get(url, headers=UA, as_json=False)
    if raw is None:
        return None
    t = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    m = NEXT_DATA.search(t)
    if not m:
        return {}
    try:
        pp = (json.loads(m.group(1)).get("props") or {}).get("pageProps") or {}
    except Exception:  # noqa: BLE001
        return {}
    return pp.get("pageData") or pp.get("initialState") or {}


def scan_join(e, details_cache):
    """JOIN (join.com): Die Firmenseite zeigt nur 5 Stellen, ihr ?page=N liefert oft die falsche Seite.
    Vollständig ist die öffentliche Liste /api/public/companies/<Firmen-ID>/jobs (pageSize nur 5 erlaubt).
    id = Nummer aus der Stellen-URL (wie im Feld seen der Tabelle); JOIN vergibt sie beim Auffrischen neu."""
    slug = e["slug"]
    base = f"https://join.com/companies/{slug}"
    key = f"join-company:{slug}"
    cid = (details_cache.get(key) or {}).get("company_id")
    first = None
    if not cid:
        first = join_page(base)
        if first is None:
            return None
        cid = (first.get("company") or {}).get("id")
        if not cid:
            err(f"JOIN {e['name']}: Firmen-ID nicht gefunden")
            return None
        details_cache[key] = {"company_id": cid, "cached_at": TODAY}
    items, page, pages, complete = [], 1, 1, True
    while page <= pages and page <= 40:
        d = http_get(f"https://join.com/api/public/companies/{cid}/jobs", {"page": page, "pageSize": 5},
                     headers={**UA, "Accept": "application/json"})
        if not isinstance(d, dict):
            complete = False
            details_cache.pop(key, None)  # Firmen-ID beim nächsten Lauf neu holen
            break
        items.extend(d.get("items") or [])
        pages = int((d.get("pagination") or {}).get("pageCount") or 1)
        page += 1
        time.sleep(0.4)
    if not items and not complete:  # API kaputt: wenigstens die ersten 5 von der Firmenseite
        first = first if first is not None else join_page(base)
        if not first:
            return None
        items = (first.get("jobs") or {}).get("items") or []
    jobs, ids = [], set()
    for it in items:
        idp = it.get("idParam") or ""
        jid = idp.split("-", 1)[0]
        if not jid.isdigit() or jid in ids:
            continue
        ids.add(jid)
        title = it.get("title") or ""
        country = ((it.get("country") or {}).get("iso3166") or "").upper()
        wp = (it.get("workplaceType") or "").upper()
        if country and country != "DE" and wp != "REMOTE":
            continue
        city = (it.get("city") or {}).get("cityName") or ""
        loc = f"Remote ({city or country or '—'})" if wp == "REMOTE" else city + (" (hybrid)" if wp == "HYBRID" else "")
        rec = {
            "id": jid,
            "source": "Сайт компании",
            "company": e["name"],
            "title": title,
            "location": loc,
            "workplace": wp.lower() or None,
            "published": (it.get("createdAt") or "")[:10] or None,
            "url": f"{base}/{idp}",
            "title_tier": title_tier(title),
            "found_by": [f"Компании под наблюдением: {e['name']}"],
        }
        if (it.get("settings") or {}).get("showSalary"):
            lo = (it.get("salaryAmountFrom") or {}).get("amount")
            hi = (it.get("salaryAmountTo") or {}).get("amount")
            if lo or hi:
                rec["salary"] = {"from": round(lo / 100) if lo else None, "to": round(hi / 100) if hi else None,
                                 "per": it.get("salaryFrequency")}
        if rec["title_tier"] == "prefer":
            c = details_cache.get(f"join:{jid}")
            if c is None:
                job = (join_page(rec["url"]) or {}).get("job") or {}
                if job:
                    c = {"description": cut(strip_html(job.get("description") or "")),
                         "updated": (job.get("updatedAt") or "")[:10] or None, "cached_at": TODAY}
                    details_cache[f"join:{jid}"] = c
                time.sleep(0.5)
            if c:
                rec["description"] = c.get("description")
                if c.get("updated"):
                    rec["updated"] = c["updated"]
        jobs.append(rec)
    if not complete:
        err(f"JOIN {e['name']}: Liste unvollständig ({len(jobs)} gelesen)")
    return jobs


def scan_ashby(e):
    """Ashby (jobs.ashbyhq.com): öffentliche Job-Board-API mit Volltext."""
    d = http_get(f"https://api.ashbyhq.com/posting-api/job-board/{e['board']}", headers={**UA, "Accept": "application/json"})
    if not isinstance(d, dict):
        return None
    jobs = []
    for j in d.get("jobs") or []:
        if j.get("isListed") is False:
            continue
        title = j.get("title") or ""
        country = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or ""
        remote = (j.get("workplaceType") or "").lower() == "remote"
        if country and not re.search(r"German|Deutschland|^DE$", country, re.I) and not remote:
            continue
        locs = [j.get("location") or ""] + [s.get("location") or "" for s in (j.get("secondaryLocations") or []) if isinstance(s, dict)]
        rec = {
            "id": f"ashby-{j.get('id')}",
            "source": "Сайт компании",
            "company": e["name"],
            "title": title,
            "location": ", ".join(x for x in locs if x),
            "workplace": (j.get("workplaceType") or "").lower() or None,
            "employment": j.get("employmentType"),
            "published": (j.get("publishedAt") or "")[:10] or None,
            "url": j.get("jobUrl"),
            "title_tier": title_tier(title),
            "found_by": [f"Компании под наблюдением: {e['name']}"],
        }
        if rec["title_tier"] == "prefer":
            rec["description"] = cut(j.get("descriptionPlain") or strip_html(j.get("descriptionHtml") or ""))
        jobs.append(rec)
    return jobs


def _title_key(t):
    t = re.sub(r"\((?:[mwfdx]\s*/\s*){1,3}[mwfdx]\)|\(all genders\)|\(alle\)", " ", (t or "").lower())
    return re.sub(r"[^0-9a-zäöüß]+", "", t)


def scan_feeds(details_cache):
    out = {}
    # JOIN zuerst: bei Firmen mit zwei Systemen (ju:niz: JOIN und Personio) bleiben die JOIN-ids (wie seen in der
    # Tabelle); vom zweiten System kommen nur Stellen mit neuem Titel dazu.
    kinds = (("join", lambda e: scan_join(e, details_cache)), ("ashby", scan_ashby), ("personio", scan_personio),
             ("recruitee", scan_recruitee), ("smartrecruiters", scan_smartrecruiters), ("workday", scan_workday))
    counts = {}
    for kind, fn in kinds:
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
                if entry["method"] != kind:  # zweites System derselben Firma: Dubletten nach Titel weglassen
                    have = {_title_key(j.get("title")) for j in entry["jobs"]}
                    jobs = [j for j in jobs if _title_key(j.get("title")) not in have]
                entry["jobs"].extend(jobs)
                entry["method"] = entry["method"] if entry["method"] == kind else f"{entry['method']}+{kind}"
                counts[kind] = counts.get(kind, 0) + len(jobs)
            time.sleep(0.3)
    STATS["feeds"] = {"companies": len(out), "jobs": counts, "failed": sorted(k for k, v in out.items() if v.get("error"))}
    log(f"Feeds: {len(out)} Firmen, Stellen {counts}")
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

# Nur Stellen in Deutschland: LinkedIn kennt «Deutschland» nicht und sucht dann am Standort des
# GitHub-Servers (USA) — darum je Seite eigener Ort (config jobspy.locations) und dieser Filter.
DE_LOC = re.compile(
    r"\b(Germany|Deutschland)\b|Baden-Württemberg|Bayern|Bavaria|Nordrhein|North Rhine|Niedersachsen|Lower Saxony|"
    r"Hessen|Hesse|Rheinland|Rhineland|Sachsen|Saxony|Thüringen|Thuringia|Schleswig|Brandenburg|Mecklenburg|Saarland|"
    r"Stuttgart|München|Munich|Köln|Cologne|Düsseldorf|Frankfurt|Hamburg|Berlin|Leipzig|Dresden|Hannover|Hanover|"
    r"Nürnberg|Nuremberg|Bremen|Ruhr|Rhein-Neckar|Rhine-Neckar|Karlsruhe|Mannheim|Heidelberg|Freiburg|Ludwigsburg|"
    r"Heilbronn|Augsburg|Bonn|Dortmund|Münster|Aachen", re.I)
NOT_DE_LOC = re.compile(r"\bUSA\b|United States|\bUK\b|United Kingdom|Canada|India|Australia", re.I)


def in_germany(loc):
    return bool(loc) and bool(DE_LOC.search(loc)) and not NOT_DE_LOC.search(loc)


LI_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
              "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}


def li_job_text(job_id):
    """Text der öffentlichen LinkedIn-Stellenseite (ohne Login); None, wenn nicht lesbar (Login-Seite, 429 …)."""
    try:
        r = requests.get(f"https://www.linkedin.com/jobs/view/{job_id}", headers=LI_HEADERS, timeout=30)
    except Exception:  # noqa: BLE001
        return None
    if r.status_code != 200 or "/jobs/view/" not in r.url:
        return None
    m = re.search(r'<div class="show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>', r.text, re.S)
    text = strip_html(m.group(1)) if m else ""
    return text or None


def scan_jobspy(details_cache=None):
    cfg = CFG.get("jobspy") or {}
    if not cfg.get("enabled", True):
        return []
    try:
        from jobspy import scrape_jobs  # type: ignore
    except Exception as ex:  # noqa: BLE001
        err(f"jobspy nicht verfügbar: {ex}")
        return []
    out = {}
    abroad = {}
    for term in cfg.get("terms", []):
        for site in cfg.get("sites", ["indeed", "linkedin"]):
            try:
                df = scrape_jobs(
                    site_name=[site],
                    search_term=term,
                    location=(cfg.get("locations") or {}).get(site) or cfg.get("location", "Deutschland"),
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
                loc = row.get("location")
                loc = "" if loc is None or isinstance(loc, float) else str(loc)
                if not in_germany(loc):
                    abroad[site] = abroad.get(site, 0) + 1
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
                    "location": loc,
                    "remote": bool(row.get("is_remote")) if row.get("is_remote") is not None else None,
                    "published": str(dp)[:10] if dp is not None and str(dp) not in ("NaT", "nan", "None") else None,
                    "url": url,
                    "title_tier": tier,
                    "description": cut(strip_html(desc)) if desc else None,
                    "found_by": [f"{site}: {term}"],
                }
            time.sleep(2)
    # LinkedIn gibt in der Suche keinen Text: Stellenseite lesen und Stellen ohne PV/BESS im Text verwerfen
    # (wie bei der Jobbörse). Seite nicht lesbar → Stelle bleibt ohne Text, wie bisher.
    # Gelesene Texte bleiben 7 Tage im details-Cache (Schlüssel "li:<id>"), damit ein zweiter Lauf am selben Tag
    # (Push, Nachstart) LinkedIn nicht erneut abfragt.
    det = {"read": 0, "cached": 0, "unreadable": 0, "no_pv_bess_dropped": 0}
    li_max = int(cfg.get("linkedin_details_max", 150))
    in_row = 0
    for url, rec in list(out.items()):
        if rec["source"] == "Indeed" and rec.get("description"):
            if not INDUSTRY_STRICT.search(f"{rec['title']} {rec['company']} {rec['description']}"):
                det["no_pv_bess_dropped"] += 1
                del out[url]
            continue
        if rec["source"] != "LinkedIn" or rec.get("description"):
            continue
        m = re.search(r"/jobs/view/(?:[^/?#]*-)?(\d+)", url)
        ck = f"li:{m.group(1)}" if m else None
        hit = details_cache.get(ck) if (details_cache is not None and ck) else None
        if hit is not None:
            det["cached"] += 1
            text = hit.get("text") or ""
        else:
            if det["read"] + det["unreadable"] >= li_max or in_row >= 6:
                continue
            text = li_job_text(m.group(1)) if m else None
            if text is None:
                det["unreadable"] += 1
                in_row += 1
                time.sleep(6)
                continue
            in_row = 0
            det["read"] += 1
            time.sleep(random.uniform(1.5, 3.5))
        relevant = bool(INDUSTRY_STRICT.search(f"{rec['title']} {rec['company']} {text}"))
        if hit is None and ck and details_cache is not None:
            details_cache[ck] = {"text": cut(text) if relevant else "", "cached_at": TODAY}
        if not relevant:
            det["no_pv_bess_dropped"] += 1
            del out[url]
        else:
            rec["description"] = cut(text)
    kept = {}
    for rec in out.values():
        kept[rec["source"]] = kept.get(rec["source"], 0) + 1
    STATS["jobspy"] = {"kept": kept, "abroad_dropped": abroad, "texts": det}
    log(f"JobSpy: {len(out)} Treffer {kept}, außerhalb Deutschlands verworfen: {abroad}, Texte: {det}")
    return list(out.values())



# ----------------------------------------------------------------------------- XING (öffentliche Jobsuche)

# Suchseite ohne Login: ~18 Karten je Begriff (nach Relevanz, nicht nach Datum). Für neue Karten wird die
# Stellenseite gelesen (JSON-LD JobPosting: Titel, Firma, Ort, Land, datePosted, Volltext) und im details-Cache
# gehalten (Schlüssel "xing:<id>"), damit jede Stelle nur einmal geladen wird.
XING_CARD = re.compile(r'<a href="(/jobs/[a-z0-9\-]+-(\d+))"[^>]*aria-label="([^"]*)"')
XING_NOISE = re.compile(r"^(Dringend gesucht|Zu den Ersten gehören|Letzte Chance|Top[- ]Job|Einfach bewerben|Job merken|Neu|Top-Job|,|\+ \d+ weitere|Externes Job-Angebot.*)$")


def xing_get(url):
    try:
        r = requests.get(url, headers=LI_HEADERS, timeout=30)
    except Exception:  # noqa: BLE001
        return None
    if r.status_code != 200:
        return None
    r.encoding = "utf-8"
    return r.text


def xing_cards(page):
    out = []
    for seg in re.split(r'(?=<a href="/jobs/[a-z0-9\-]+-\d+")', page)[1:]:
        m = XING_CARD.match(seg)
        if not m:
            continue
        title = html.unescape(m.group(3)).strip()
        txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", seg[:8000], flags=re.S)
        parts = [html.unescape(p).strip() for p in re.sub(r"<[^>]+>", "|", txt).split("|")]
        parts = [p for p in parts if p and p != "\xa0" and p != title and not XING_NOISE.match(p)]
        out.append({"path": m.group(1), "id": m.group(2), "title": title,
                    "company": parts[0] if parts else "", "location": parts[1] if len(parts) > 1 else ""})
    return out


def xing_details(page):
    """Felder aus dem JSON-LD JobPosting der Stellenseite; None, wenn keins da ist."""
    for block in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', page or "", re.S):
        try:
            d = json.loads(block)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(d, list):
            d = next((x for x in d if isinstance(x, dict) and x.get("@type") == "JobPosting"), None)
        if not isinstance(d, dict) or d.get("@type") != "JobPosting":
            continue
        org = d.get("hiringOrganization") if isinstance(d.get("hiringOrganization"), dict) else {}
        locs = d.get("jobLocation") or []
        locs = [locs] if isinstance(locs, dict) else locs
        places, countries = [], set()
        for loc in locs:
            a = (loc or {}).get("address") or {}
            if a.get("addressCountry"):
                countries.add(str(a["addressCountry"]).upper())
            place = ", ".join(x for x in (a.get("addressLocality"), a.get("addressRegion")) if x)
            if place:
                places.append(place)
        desc = strip_html(html.unescape(d.get("description") or ""))
        desc = re.sub(r"^\s*null\s*", "", desc)
        return {"title": d.get("title"), "company": org.get("name"), "location": "; ".join(places),
                "countries": sorted(countries), "published": (d.get("datePosted") or "")[:10] or None,
                "description": desc, "cached_at": TODAY}
    return None


def scan_xing(details_cache):
    cfg = CFG.get("xing") or {}
    if not cfg.get("enabled", True):
        return []
    terms = cfg.get("terms") or (CFG.get("jobspy") or {}).get("terms", [])
    cutoff = (NOW - dt.timedelta(days=int(cfg.get("max_age_days", 14)))).date().isoformat()
    max_details = int(cfg.get("max_details", 150))
    st = {"searches": 0, "search_failed": 0, "cards": 0, "unique": 0, "title_dropped": 0, "details_read": 0,
          "details_cached": 0, "details_failed": 0, "abroad_dropped": 0, "old_dropped": 0, "no_pv_bess_dropped": 0, "kept": 0}
    found, fails = {}, 0
    for term in terms:
        if fails >= 3:
            err("XING: Suche dreimal hintereinander ohne Antwort — abgebrochen")
            break
        page = xing_get("https://www.xing.com/jobs/search?" + urlencode({"keywords": term, "location": cfg.get("location", "Deutschland")}))
        st["searches"] += 1
        if not page:
            st["search_failed"] += 1
            fails += 1
            time.sleep(5)
            continue
        fails = 0
        for c in xing_cards(page):
            st["cards"] += 1
            if c["id"] in found:
                if term not in found[c["id"]]["terms"]:
                    found[c["id"]]["terms"].append(term)
            else:
                found[c["id"]] = {**c, "terms": [term]}
        time.sleep(random.uniform(2, 4))
    st["unique"] = len(found)
    out, in_row = [], 0
    for xid, c in found.items():
        tier = title_tier(c["title"])
        if tier == "drop":
            st["title_dropped"] += 1
            continue
        det = details_cache.get(f"xing:{xid}")
        if det:
            st["details_cached"] += 1
        elif st["details_read"] + st["details_failed"] < max_details and in_row < 6:
            det = xing_details(xing_get("https://www.xing.com" + c["path"]))
            if det:
                st["details_read"] += 1
                in_row = 0
                details_cache[f"xing:{xid}"] = det
            else:
                st["details_failed"] += 1
                in_row += 1
            time.sleep(random.uniform(1.5, 3.5))
        det = det or {}
        if det.get("countries") and "DE" not in det["countries"]:
            st["abroad_dropped"] += 1
            continue
        if det.get("published") and det["published"] < cutoff:
            st["old_dropped"] += 1
            continue
        company = det.get("company") or c["company"]
        if det.get("description") and not INDUSTRY_STRICT.search(f"{c['title']} {company} {det['description']}"):
            st["no_pv_bess_dropped"] += 1
            continue
        out.append({"id": f"xing-{xid}", "source": "XING", "title": det.get("title") or c["title"], "company": company,
                    "location": det.get("location") or c["location"], "published": det.get("published"),
                    "url": "https://www.xing.com" + c["path"], "title_tier": tier,
                    "description": cut(det["description"]) if det.get("description") else None,
                    "found_by": [f"xing: {t}" for t in c["terms"]]})
    st["kept"] = len(out)
    STATS["xing"] = st
    log(f"XING: {st}")
    return out


# ----------------------------------------------------------------------------- SerpApi (Google for Jobs)

SERP_AGG = re.compile(r"linkedin\.|indeed\.|stepstone\.|xing\.|glassdoor\.|kununu\.|jooble|jobijoba|adzuna|talent\.com|jobrapido|neuvoo|kimeta|careerjet|monster\.|jobware|joblift|bebee|trabajo\.org|kleinanzeigen|hokify|heyjobs|joblocal|studysmarter|learn4good|simplyhired|jobleads|jobtensor|womenforhire|salutemyjob|mygermanjobs|job\.fish|stellenanzeigen\.de|stellenonline|jobvector|yourfirm|jobs\.de|jobbörse|jobboerse|meinestadt|regio-jobanzeiger|rejobs|greenjobs|eejobs|solarjob|energiejobs|whatjobs|jobisjob|jobs\.ch|arbeitsagentur|ebay|facebook|google\.com|stevenagefc|jobijoba|jobsora|jooble|expertini|de\.jobted|jobted|ziprecruiter|grabjobs|workwise|campusjaeger|absolventa|gigajob|jobsuche", re.I)


def _norm_key(s):
    return re.sub(r"[^a-z0-9äöüß]+", " ", (s or "").lower()).strip()


def _best_link(links, company):
    """Bevorzugt die Arbeitgeber-Seite: Domain enthält ein Wort des Firmennamens; sonst erste Nicht-Aggregator-Seite."""
    toks = [t for t in re.split(r"[^a-z0-9]+", (company or "").lower()) if len(t) >= 4 and t not in ("gmbh", "group", "gruppe", "solar", "energy", "energie", "deutschland", "germany")]
    best, best_score = None, -1
    for a in links:
        link = a.get("link") or ""
        host = re.sub(r"^https?://(www\.)?", "", link).split("/")[0].lower()
        if any(t in host for t in toks):
            score = 2
        elif not SERP_AGG.search(host + " " + str(a.get("title", ""))):
            score = 1
        else:
            score = 0
        if score > best_score:
            best, best_score = a, score
    return best or {}


def _rel_date(txt):
    """'vor 3 Tagen' / '3 days ago' / 'vor 5 Stunden' → ISO-Datum; None wenn unbekannt."""
    if not txt:
        return None
    t = str(txt).lower()
    m = re.search(r"(\d+)\s*(tag|day|stunde|hour|minute|woche|week|monat|month)", t)
    if not m:
        if "heute" in t or "today" in t or "gerade" in t or "just" in t:
            return TODAY
        if "gestern" in t or "yesterday" in t:
            return (NOW - dt.timedelta(days=1)).date().isoformat()
        return None
    n, unit = int(m.group(1)), m.group(2)
    days = {"tag": n, "day": n, "stunde": 0, "hour": 0, "minute": 0, "woche": 7 * n, "week": 7 * n, "monat": 30 * n, "month": 30 * n}[unit]
    return (NOW - dt.timedelta(days=days)).date().isoformat()


def scan_serpapi():
    """Google for Jobs über SerpApi (Free-Plan: 250 Suchen/Monat). Pro Tag höchstens `per_day` Suchen,
    höchstens einmal am Tag (Pushes lösen den Workflow mehrfach aus), Abfragen rotieren über die Tage."""
    cfg = CFG.get("serpapi") or {}
    key = os.environ.get("SERPAPI_KEY", "").strip()
    state = load_json("serp-log.json", {})
    st = {"configured": bool(key), "used_today": 0, "results": 0}
    STATS["serpapi"] = st
    if key:
        SECRETS.append(key)
    if not cfg.get("enabled", True) or not key:
        log("SerpApi: kein Schlüssel (Secret SERPAPI_KEY) – übersprungen")
        return []
    cache = load_json("serp-results.json", {})
    if state.get("last_date") == TODAY and not os.environ.get("SERPAPI_FORCE"):
        if cache.get("date") == TODAY and isinstance(cache.get("items"), list):
            log(f"SerpApi: heute schon gelaufen ({state.get('used_today')} Suchen) – {len(cache['items'])} Treffer aus dem Tages-Cache")
            st.update({"skipped": "already today", "left": state.get("left"), "results": len(cache["items"]), "from_cache": True})
            return cache["items"]
        log("SerpApi: heute schon gelaufen, aber kein Tages-Cache – Suche wird wiederholt (gleiche Abfragen sind bei SerpApi 1 h lang kostenlos)")
    base = "https://serpapi.com/search.json"
    # Kontostand (kostenlos)
    acct = http_get("https://serpapi.com/account.json", {"api_key": key}, headers=UA, tries=2)
    left = None
    if isinstance(acct, dict):
        left = acct.get("total_searches_left", acct.get("plan_searches_left"))
        st["left"] = left
        st["plan"] = acct.get("plan_name")
    reserve = int(cfg.get("reserve", 10))
    if left is not None and int(left) <= reserve:
        err(f"SerpApi: nur noch {left} Suchen übrig – übersprungen")
        state.update({"last_date": TODAY, "used_today": 0, "left": left})
        save_json("serp-log.json", state)
        return []
    # Suchläufe: bundesweit und lokal (Stuttgart + Umkreis), Abfragen rotieren tageweise;
    # solange viel Guthaben da ist (Bonus-Suchen), doppelt so viele pro Tag
    passes = cfg.get("passes") or [{"label": "", "location": cfg.get("location"), "queries": cfg.get("queries") or [], "per_day": int(cfg.get("per_day", 7))}]
    boost = 2 if (left is not None and int(left) > int(cfg.get("boost_above", 400))) else 1
    budget = (int(left) - reserve) if left is not None else 10 ** 6
    doy = NOW.timetuple().tm_yday
    plan = []  # (label, location, lrad, query)
    for p in passes:
        qs = p.get("queries") or []
        n = min(len(qs), int(p.get("per_day", 5)) * boost)
        if not qs or n <= 0:
            continue
        start = (doy * n) % len(qs)
        for i in range(n):
            plan.append((p.get("label") or "", p.get("location"), p.get("lrad"), qs[(start + i) % len(qs)]))
    plan = plan[:max(0, budget)]
    if not plan:
        return []
    todays = [f"{lab}: {q}" if lab else q for lab, _, _, q in plan]
    uds = state.get("uds_3days")
    common = {"engine": "google_jobs", "api_key": key, "gl": cfg.get("gl", "de"), "hl": cfg.get("hl", "de"),
              "google_domain": cfg.get("google_domain", "google.de")}
    out = {}
    dup_index = {}
    used = 0
    stale_hits = 0
    for label, location, lrad, q in plan:
        params = dict(common, q=q)
        if location:
            params["location"] = location
        if lrad:
            params["lrad"] = lrad
        if uds:
            params["uds"] = uds
        tag = f"Google Jobs {label}: {q}" if label else f"Google Jobs: {q}"
        d = http_get(base, params, headers=UA, tries=2, timeout=60)
        used += 1
        if not isinstance(d, dict):
            continue
        if d.get("error") and not d.get("jobs_results"):
            # "Google hasn't returned any results" ist kein Fehler – nur leer
            if "returned any results" not in str(d.get("error")):
                err(f"SerpApi «{q}»: {str(d.get('error'))[:120]}")
            continue
        if "filters_seen" not in st:
            st["filters_seen"] = [{"name": f.get("name"), "options": [o.get("name") for o in (f.get("options") or [])][:8]} for f in (d.get("filters") or []) if isinstance(f, dict)][:8]
            st["response_keys"] = sorted(k for k in d.keys() if not k.startswith("search_"))
        # Datumsfilter (uds) einmalig aus den Filtern merken: "Letzte 3 Tage" / "Last 3 days"
        # (der Wert steht je nach Layout in `uds` oder nur im serpapi_link / link als Parameter uds=)
        if not uds:
            from urllib.parse import unquote, parse_qs, urlparse
            for f in d.get("filters") or []:
                for o in (f.get("options") or []):
                    if not isinstance(o, dict):
                        continue
                    nm = str(o.get("name") or "")
                    if not re.search(r"\b3\b", nm):
                        continue
                    if "filter_option_keys" not in st:
                        st["filter_option_keys"] = sorted(o.keys())
                    val = o.get("uds")
                    if not val:
                        for lk in (o.get("serpapi_link"), o.get("link")):
                            if lk and "uds=" in str(lk):
                                val = (parse_qs(urlparse(str(lk)).query).get("uds") or [None])[0]
                                if val:
                                    break
                    if val:
                        uds = unquote(val) if "%" in val else val
                        state["uds_3days"] = uds
                        state["uds_name"] = nm
                        log(f"SerpApi: Datumsfilter «{nm}» gemerkt")
                        break
                if uds:
                    break
        jobs = d.get("jobs_results") or []
        old = 0
        for j in jobs:
            if not isinstance(j, dict):
                continue
            title = (j.get("title") or "").strip()
            jid = j.get("job_id") or f"{title}|{j.get('company_name')}"
            key_id = "serp-" + hashlib.sha1(str(jid).encode("utf-8")).hexdigest()[:16]
            if key_id in out:
                if tag not in out[key_id]["found_by"]:
                    out[key_id]["found_by"].append(tag)
                continue
            tier = title_tier(title)
            det = j.get("detected_extensions") or {}
            posted = _rel_date(det.get("posted_at") or " ".join(j.get("extensions") or []))
            if posted and posted < (NOW - dt.timedelta(days=int(cfg.get("max_age_days", 10)))).date().isoformat():
                old += 1
                continue
            if tier == "drop":
                continue
            desc = strip_html(j.get("description") or "")
            if not INDUSTRY.search(title + " " + desc[:3000]):
                continue
            links = [a for a in (j.get("apply_options") or []) if isinstance(a, dict) and a.get("link")]
            company = (j.get("company_name") or "").strip()
            # dieselbe Stelle über mehrere Jobbörsen: zusammenführen (Titel + Firma)
            dup_key = _norm_key(title) + "|" + _norm_key(company)
            twin = dup_index.get(dup_key)
            if twin:
                have = {a.get("link") for a in twin["apply_links"]}
                twin["apply_links"].extend({"title": a.get("title"), "link": a.get("link")} for a in links if a.get("link") not in have)
                twin["apply_links"] = twin["apply_links"][:8]
                twin["url"] = _best_link([{"title": a.get("title"), "link": a.get("link")} for a in twin["apply_links"]], company).get("link") or twin["url"]
                if tag not in twin["found_by"]:
                    twin["found_by"].append(tag)
                if not twin.get("published") and posted:
                    twin["published"] = posted
                continue
            best = _best_link(links, company)
            out[key_id] = {
                "id": key_id,
                "source": "Google Jobs",
                "via": j.get("via"),
                "title": title,
                "company": (j.get("company_name") or "").strip(),
                "location": j.get("location"),
                "published": posted,
                "posted_at": det.get("posted_at"),
                "schedule": det.get("schedule_type"),
                "work_from_home": det.get("work_from_home"),
                "salary": det.get("salary"),
                "url": best.get("link") or j.get("share_link"),
                "apply_links": [{"title": a.get("title"), "link": a.get("link")} for a in links[:6]],
                "share_link": j.get("share_link"),
                "description": cut(desc) if desc else None,
                "highlights": [{"title": h.get("title"), "items": (h.get("items") or [])[:12]} for h in (j.get("job_highlights") or []) if isinstance(h, dict)][:4],
                "title_tier": tier,
                "found_by": [tag],
            }
            dup_index[dup_key] = out[key_id]
        if uds and jobs and old == len(jobs):
            stale_hits += 1
        time.sleep(1.5)
    if uds and stale_hits and stale_hits == used:
        # Filter wirkt offenbar nicht mehr – nächstes Mal neu ermitteln
        state.pop("uds_3days", None)
        err("SerpApi: Datumsfilter lieferte nur alte Treffer – wird neu ermittelt")
    st.update({"used_today": used, "results": len(out), "queries": todays, "uds": bool(uds), "boost": boost})
    state.update({"last_date": TODAY, "used_today": used, "left": (int(left) - used) if left is not None else None,
                  "month": TODAY[:7], "used_month": (state.get("used_month", 0) if state.get("month") == TODAY[:7] else 0) + used})
    save_json("serp-log.json", state)
    save_json("serp-results.json", {"date": TODAY, "items": list(out.values())})
    log(f"SerpApi: {used} Suchen, {len(out)} Treffer, übrig ≈ {state.get('left')}")
    return list(out.values())


# ----------------------------------------------------------------------------- main

def main():
    details_cache = load_json("details.json", {})
    seen = load_json("seen.json", {"refnr": {}})

    prefer, other, alljobs = scan_jobboerse(details_cache)
    watch = scan_employers(int(CFG.get("employer_days", 30)), details_cache)
    for cid, entry in scan_feeds(details_cache).items():
        cur = watch.get(cid)
        if cur:
            cur["jobs"].extend(entry.get("jobs", []))
            cur["method"] = f"{cur['method']}+{entry['method']}"
            if entry.get("error"):
                cur["error"] = entry["error"]
        else:
            watch[cid] = entry
    boards = scan_atom()
    boards += scan_jobspy(details_cache)
    boards += scan_xing(details_cache)
    boards += scan_serpapi()

    # beobachtete Firmen: ausgeschlossene Titel (Monteure, Vertrieb, IT …) nicht in die Datei
    for entry in watch.values():
        allj = entry.get("jobs") or []
        entry["jobs_total"] = len(allj)
        entry["jobs"] = [j for j in allj if j.get("title_tier") != "drop"]

    # neu / schon gesehen (nur Information; die Tabelle gleicht selbst ab)
    for rec in prefer + other:
        rec["seen_before"] = rec["refnr"] in seen["refnr"]
        seen["refnr"][rec["refnr"]] = TODAY
    # seen.json und details.json auf 45 Tage begrenzen
    cutoff = (NOW - dt.timedelta(days=45)).date().isoformat()
    seen["refnr"] = {k: v for k, v in seen["refnr"].items() if v >= cutoff}
    li_cutoff = (NOW - dt.timedelta(days=7)).date().isoformat()  # LinkedIn-Texte nur 7 Tage
    details_cache = {k: v for k, v in details_cache.items()
                     if (v.get("cached_at") or "") >= (li_cutoff if k.startswith("li:") else cutoff)}

    out = {
        "generated": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": TODAY,
        "days": CFG.get("days", 7),
        "schema": {
            "candidates": "Jobbörse, Titel passt zur Rolle (prefer): mit description",
            "other": "Jobbörse, Titel weder ausgeschlossen noch passend: nur Titel/Firma/Ort — kurz durchsehen",
            "watch": "beobachtete Firmen (Jobbörse nach Name, Personio, Recruitee, SmartRecruiters, Workday, JOIN, Ashby): id → {name, method, jobs[]}; jobs mit title_tier prefer haben description; JOIN: id = Nummer aus der Stellen-URL, Liste vollständig (alle Seiten)",
            "boards": "eejobs/greenjobs (Atom), JobSpy (Indeed/LinkedIn), XING (öffentliche Suche) und Google Jobs (SerpApi); description = Volltext der Stelle (Google Jobs immer, LinkedIn/Indeed/XING wenn lesbar, PV/BESS darin geprüft) — dann danach bewerten, keine Kopie suchen; nur Titel, die nicht ausgeschlossen sind",
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
