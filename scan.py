#!/usr/bin/env python3
"""Täglicher Vakanzen-Scan für die Jobsuche-Tabelle.

Quellen:
  * Arbeitsagentur Jobsuche-API (dokumentiert von bundesAPI/jobsuche-api, öffentlicher Client-Key)
    - Branchen-Suchwörter × (Deutschland | 75 km um Ludwigsburg | Homeoffice), Fenster N Tage
    - Arbeitgeber aus dem Beobachtungs-Verzeichnis (Suche nach Name)
  * Personio-XML-Feeds, Recruitee-, SmartRecruiters-, Workday- und Ashby-APIs, JOIN-Firmenseiten und Karriereseiten
    mit Sitemap + JSON-LD (EnBW, Netze BW) beobachteter Firmen
  * Atom-Feeds von eejobs.de und greenjobs.de
  * optional python-jobspy: LinkedIn-Gastsuche (enge Begriffe) und Indeed (enge Begriffe + breite Branchenwörter)
  * XING, öffentliche Suche: letzte 24 Stunden (enge Begriffe + breite Branchenwörter), letzte Woche (enge Begriffe)
  * Google for Jobs über SerpApi
Die Quellen laufen nebeneinander (Threads); fällt eine aus, läuft der Rest weiter.

Ergebnis: data/today.json (+ seen.json, details.json, run-log.json, history.json, dropped.json, disputed.json).
tools/tokens.py und tools/mailmatch.py lädt die Morgenaufgabe direkt aus dem Repository.
Keine Zugangsdaten, keine personenbezogenen Daten.
"""
import base64
import concurrent.futures as cf
import hashlib
import datetime as dt
import html
import json
import os
import random
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET

import requests
from urllib import robotparser
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

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
SOFT = re.compile(CFG["title_soft"], re.I) if CFG.get("title_soft") else None
# Schalter: die Regel gilt erst, wenn auch die Morgenaufgabe solche Titel behält — sonst sähe sie die Stelle einmal,
# verwürfe sie nach ihrer eigenen Titelregel («Senior») und bekäme sie wegen first_seen nie wieder
if not CFG.get("title_senior_optional_on", True):
    CFG["title_senior_optional"] = None
SENIOR_OPT = re.compile(CFG["title_senior_optional"], re.I) if CFG.get("title_senior_optional") else None
# Beleg im Stellentext für Titel mit «weichem» Ausschlusswort: eindeutige PV-/Speicherwörter (ohne «Netzanschluss», «Speicher»,
# «Erneuerbare» — die stehen auch in Gas-, Wasser- und Stadtwerke-Texten)
PROOF = re.compile(CFG.get("title_soft_proof") or CFG.get("pv_only_regex") or CFG.get("industry_strict_regex") or CFG["industry_regex"], re.I)
GERMANY = {"DE", "DEU", "DEUTSCHLAND", "GERMANY", "ALLEMAGNE", "GERMANIA", "ALEMANIA", "DUITSLAND", "NIEMCY"}
# Seiten, auf denen oft längst geschlossene oder nie vorhandene Stellen stehen: ein Treffer nur von dort zählt nicht allein
STALE_HOSTS = tuple(h.lower() for h in CFG.get("stale_hosts") or [])


def is_stale(url):
    host = re.sub(r"^https?://(www\.)?", "", url or "").split("/")[0].lower()
    return any(host == h or host.endswith("." + h) for h in STALE_HOSTS)


def mark_stale(boards, records):
    """Treffer von STALE_HOSTS: gibt es dieselbe Stelle (Titel + Firma) schon aus einer anderen Quelle, fällt er weg;
    sonst bekommt er verify=true — die Morgenaufgabe sucht eine zweite Fundstelle und nimmt die Stelle ohne sie nicht auf."""
    key = lambda r: _norm_key(r.get("title")) + "|" + _norm_key(r.get("company"))
    known = {key(r) for r in records} | {key(b) for b in boards if not is_stale(b.get("url"))}
    kept, st = [], {"flagged": 0, "dup_dropped": 0}
    for b in boards:
        if is_stale(b.get("url")):
            if key(b) in known:
                st["dup_dropped"] += 1
                continue
            b["verify"] = True
            st["flagged"] += 1
        kept.append(b)
    return kept, st


def _utc(s):
    if not s:
        return None
    d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _berlin_offset(t):
    """Abstand Berlin–UTC zum Zeitpunkt t (EU-Regel: Sommerzeit vom letzten Sonntag im März bis zum letzten Sonntag im
    Oktober, jeweils 01:00 UTC) — ohne Zeitzonen-Datenbank."""
    def last_sunday(month):
        d = dt.date(t.year, month, 31)
        return dt.datetime(d.year, d.month, d.day - (d.weekday() + 1) % 7, 1, tzinfo=dt.timezone.utc)
    return dt.timedelta(hours=2 if last_sunday(3) <= t < last_sunday(10) else 1)


def morning_before(t):
    """Start der letzten Morgenaufgabe vor dem Zeitpunkt t (config morning_run_berlin, Ortszeit Berlin)."""
    hh, mm = (int(x) for x in str(CFG.get("morning_run_berlin", "10:53")).split(":"))
    loc = (t + _berlin_offset(t)).replace(tzinfo=None)
    m = loc.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if m > loc:
        m -= dt.timedelta(days=1)
    u = m.replace(tzinfo=dt.timezone.utc) - dt.timedelta(hours=1)
    return m.replace(tzinfo=dt.timezone.utc) - _berlin_offset(u)


def stamp_first_seen(items, smap, pending, prev_keys, prev_gen, stamp):
    """first_seen (Watch-Stellen, Boards): wann der Scanner die Stelle zum ersten Mal sah — die Morgenaufgabe nimmt nur
    Stellen mit first_seen nach ihrem letzten Lauf. items: [(Schlüssel, Stelle)]; smap: Schlüssel → first_seen;
    pending: Schlüssel → first_seen der Stellen, die noch in keiner Datei standen, die die Morgenaufgabe gelesen hat.
    Hat seit der vorigen Datei (prev_keys, prev_gen) eine Morgenaufgabe begonnen, so hat sie diese Datei gelesen: deren
    Stellen sind übergeben. Eine Stelle, die bei der Morgenaufgabe fehlte (Quelle fiel aus, Liste unvollständig — oder sie
    kam in einem Zwischenlauf am Nachmittag und war am nächsten Morgen nicht in der Datei), bekommt beim Wiederauftauchen
    den Zeitstempel dieses Laufs — sonst ginge sie mit ihrem alten Stempel nie mehr an die Morgenaufgabe."""
    now = _utc(stamp)
    m_last = morning_before(now)
    pg = _utc(prev_gen)
    if pending is None:  # erster Lauf mit dieser Liste: offen ist, was nach der Morgenaufgabe vor der vorigen Datei kam
        cut = morning_before(pg) if pg else m_last
        pending = {k: v for k, v in smap.items() if _utc(v) > cut}
    if pg and m_last > pg:
        for k in [k for k in pending if k in prev_keys]:
            del pending[k]
    again = 0
    for k, it in items:
        s = smap.get(k)
        if s is None or (k in pending and _utc(s) < m_last):
            again += s is not None
            smap[k] = pending[k] = stamp
        it["first_seen"] = smap[k]
    return pending, again
PV_ONLY = re.compile(CFG.get("pv_only_regex") or CFG.get("industry_strict_regex") or CFG["industry_regex"], re.I)
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


def senior_optional(t):
    """«Senior» steht im Titel nur als Möglichkeit (config title_senior_optional): «(Senior) Projektmanager»,
    «Projektmanager (Junior/Senior)», «Projektleiter / Senior Projektleiter» — die Stelle ist auch ohne «Senior»
    ausgeschrieben. Ein weiteres, nicht eingeklammertes «Senior» im selben Titel zählt weiter als Ausschluss."""
    if not SENIOR_OPT or not SENIOR_OPT.search(t):
        return False
    return not re.search(r"\bsenior|\bsr\.", SENIOR_OPT.sub(" ", t), re.I)


def soft_only(t, hits):
    """Alle Ausschlusstreffer des Titels sind «weich» (config title_soft; «Senior» neben «Junior» oder nur als Möglichkeit,
    siehe senior_optional) und der Titel nennt eine Rolle — z. B. «Montage-Disponent Photovoltaik»,
    «Kundenservice & Backoffice – Photovoltaik», «(Senior) Projektmanager Photovoltaik»."""
    if not hits or not PREF.search(t):
        return False
    junior = bool(re.search(r"junior", t, re.I)) or senior_optional(t)
    return all(bool(SOFT and SOFT.fullmatch(h)) or bool(junior and re.match(r"senior|sr\.", h, re.I)) for h in hits)


def title_tier(title):
    """'drop' | 'prefer' | 'other'. Titel, deren Ausschlusswörter alle «weich» sind (soft_only), fallen seit 15.11.2026
    nicht mehr durch: sie laufen als 'prefer' weiter (dadurch mit Stellentext), und main() behält sie nur mit
    PV-/Speicherbezug (soft_keep) — sonst werden sie wie bisher verworfen."""
    t = title or ""
    hits = [m.group(0) for m in EXCL.finditer(t)]
    if hits and not soft_only(t, hits):
        return "drop"
    # Nur Wind/Wärme/Laden/Netzbau im Titel ohne PV- oder Speicherwort: raus (PV oder BESS muss zur Rolle gehören)
    if ADJACENT and ADJACENT.search(t) and not (CORE and CORE.search(t)):
        return "drop"
    if PREF.search(t):
        return "prefer"
    return "other"


def soft_keep(rec, company=None, own_feed=False):
    """Für Stellen, die nur dank soft_only durch den Titelfilter kamen. None: kein solcher Titel. Sonst der Beleg für den
    PV-/Speicherbezug: 'feed' — eigene Stellenliste einer beobachteten Firma; 'title' — Wort aus title_core im Titel oder
    Firmennamen; 'text' — Wort aus title_soft_proof im Stellentext; '' — kein Beleg (verwerfen)."""
    title = rec.get("title") or ""
    if not EXCL.search(title):
        return None
    if own_feed:
        return "feed"
    if CORE and CORE.search(f"{title} {rec.get('company') or ''} {company or ''}"):
        return "title"
    text = " ".join(str(rec.get(k) or "") for k in ("description", "beruf", "employer_profile"))
    return "text" if PROOF.search(text) else ""


DROPPED = []  # nach Titel verworfene Stellen dieses Laufs (data/dropped.json)


def drop_why(title):
    """Wörter, an denen der Titel scheiterte; role: ohne sie bleibt noch ein Rollenwort im Titel (strittig);
    soft: alle diese Wörter sind «weich» (soft_only) — verworfen wurde die Stelle dann, weil der PV-/Speicherbezug fehlt."""
    t = title or ""
    hits = [m.group(0) for m in EXCL.finditer(t)]
    why = sorted({h.lower() for h in hits})
    adj = ADJACENT.search(t) if ADJACENT and not (CORE and CORE.search(t)) else None
    if not why and adj:
        return ["nur " + adj.group(0).lower()], False, False
    role = bool(PREF.search(EXCL.sub(" ", t)))
    if adj and soft_only(t, hits):  # die weichen Wörter allein hätten ihn nicht verworfen — der Grund ist das Nachbarthema
        return why + ["nur " + adj.group(0).lower()], role, False
    return why, role, soft_only(t, hits)


def note_drop(title, company, source, extra=None):
    why, role, soft = drop_why(title)
    if extra:
        why = why + [extra]
    DROPPED.append({"t": (title or "")[:140], "c": (company or "")[:60], "s": source, "w": why, "role": role, "soft": soft})


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
            note_drop(rec["title"], rec.get("company"), "Jobbörse")
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
    posts, offset = [], 0
    while offset < 1000:
        params = {"limit": 100, "offset": offset}
        if e.get("country"):
            params["country"] = e["country"]
        d = http_get(base, params, headers=UA)
        if not d:
            if offset == 0:
                return None
            break
        content = d.get("content") or []
        posts.extend(content)
        offset += 100
        if not content or offset >= int(d.get("totalFound") or 0):
            break
        time.sleep(0.3)
    jobs = []
    for p in posts:
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


# Workday steht hinter Cloudflare. Seit 16.11.2026 beantwortet es Anfragen von GitHub-Servern oft mit 429 und
# «Cf-Mitigated: challenge» (Bot-Schutz, kein Mengenlimit) — bei 8 von 10 Firmen, EWE (wd116) kam durch. Den Schutz
# umgehen wir nicht: je Liste ein Versuch im gemeinsamen Takt (config workday_gap), nach zwei abgewiesenen Anfragen in
# Folge keine weiteren; die abgewiesenen Firmen nach den übrigen Quellen (config workday_retry_s) noch einmal genauso.
# Echtes Mengenlimit (429 ohne Challenge): warten (Retry-After) und wiederholen. Beschreibungen kommen aus dem Cache.
# Eine Liste, die nicht vollständig lud, gilt als nicht geladen (Firma in watch_failed, wenn sie sonst nichts hat).
WD = {"t": 0.0, "t429": 0.0, "fails": 0, "off": False, "patient": False, "status": None}
WD_GAP = float(CFG.get("workday_gap", 2.0))


def wd_stats():
    return STATS.setdefault("workday", {"requests": 0, "blocked": 0, "429": 0, "waited_s": 0, "details_read": 0,
                                        "details_cached": 0, "details_failed": 0, "deferred": [], "failed": []})


def wd_call(url, body=None):
    """Eine Workday-Anfrage (POST mit body, sonst GET) im gemeinsamen Takt → JSON oder None; WD["status"]: ok, gone
    (404/410), blocked (Cloudflare-Challenge), 429 (Mengenlimit, auch nach Warten), error."""
    st = wd_stats()
    if WD["off"]:
        WD["status"] = "blocked"
        return None
    h = dict(UA)
    h["Accept"] = "application/json"
    waits = (20, 45, 90) if WD["patient"] else (10, 25)
    last = None
    for i in range(len(waits) + 1):
        pause = WD_GAP - (time.time() - WD["t"])
        if pause > 0:
            time.sleep(pause)
        WD["t"] = time.time()
        st["requests"] += 1
        try:
            r = (requests.post(url, json=body, headers=h, timeout=40) if body is not None
                 else requests.get(url, headers=h, timeout=40))
        except Exception as ex:  # noqa: BLE001
            last = type(ex).__name__
            time.sleep(3)
            continue
        if r.headers.get("Cf-Mitigated") or (r.status_code == 403 and "cloudflare" in (r.headers.get("Server") or "").lower()):
            st["blocked"] += 1
            WD["t429"] = time.time()
            last = "blocked"
            break
        if r.status_code == 429:
            st["429"] += 1
            WD["t429"] = time.time()
            last = "429"
            if i == len(waits):
                break
            ra = (r.headers.get("Retry-After") or "").strip()
            w = min(int(ra), 120) if ra.isdigit() else waits[i]
            st["waited_s"] += w
            time.sleep(w)
            continue
        if r.status_code in (404, 410):
            WD["status"] = "gone"
            return None
        if r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            time.sleep(5)
            continue
        if r.status_code >= 400:
            last = f"HTTP {r.status_code}"
            break
        try:
            d = r.json()
        except ValueError:
            last = "kein JSON"
            break
        WD["fails"] = 0
        WD["status"] = "ok"
        return d
    if last in ("blocked", "429"):
        WD["status"] = last
        WD["fails"] += 1
        if WD["fails"] >= 2:
            WD["off"] = True
        return None  # Firma in den zweiten Durchgang bzw. in watch_failed; Fehlereintrag macht scan_feeds
    WD["status"] = "error"
    err(f"{'POST' if body is not None else 'GET'} {url}: {last}")
    return None


def workday_facets(url, rx):
    """Facetten-Werte (Land, Standort), deren Name auf rx passt → {facetParameter: [ids]} für appliedFacets;
    None, wenn die Liste nicht lud."""
    d = wd_call(url, {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    if d is None:
        return None
    applied = {}

    def walk(node, param):
        for v in node.get("values") or []:
            if not isinstance(v, dict):
                continue
            if v.get("values"):
                walk(v, v.get("facetParameter") or param)
            elif v.get("id") and rx.search(v.get("descriptor") or ""):
                applied.setdefault(param, []).append(v["id"])
    for f in d.get("facets") or []:
        walk(f, f.get("facetParameter"))
    return applied


def scan_workday(e, details_cache):
    """Workday-Liste (öffentliche JSON-Schnittstelle der Karriereseite). None, wenn eine Seite der Liste nicht lud —
    im ersten Durchgang auch, wenn eine Beschreibung abgewiesen wurde (dann zweiter Durchgang, siehe scan_feeds)."""
    host, tenant, site = e["host"], e["tenant"], e["site"]
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    st = wd_stats()
    jobs = []
    offset, total = 0, None
    locf = re.compile(e["location_regex"], re.I) if e.get("location_regex") else None
    facets = {}
    if e.get("facet_match"):
        facets = workday_facets(url, re.compile(e["facet_match"], re.I))
        if facets is None:
            return None
        if not facets:
            return []  # in diesem Land gerade keine Stellen (der Wert fehlt in der Facette)
    while offset < 400:
        d = wd_call(url, {"appliedFacets": facets, "limit": 20, "offset": offset, "searchText": e.get("searchText", "")})
        if d is None:
            return None
        if total is None:
            total = int(d.get("total") or 0)
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
                ck = f"wd:{rec['url']}"
                c = details_cache.get(ck)
                if c:
                    st["details_cached"] += 1
                else:
                    det = wd_call(f"https://{host}/wday/cxs/{tenant}/{site}{path}")
                    info = (det or {}).get("jobPostingInfo") or {}
                    if info:
                        c = {"description": cut(strip_html(info.get("jobDescription") or "")),
                             "published": info.get("startDate"), "cached_at": TODAY}
                        details_cache[ck] = c
                        st["details_read"] += 1
                    elif WD["status"] in ("blocked", "429") and not WD["patient"]:
                        return None  # Beschreibung abgewiesen: Firma im zweiten Durchgang noch einmal
                    elif WD["status"] != "gone":
                        st["details_failed"] += 1
                if c:
                    rec["description"] = c.get("description")
                    rec["published"] = c.get("published") or rec["published"]
            jobs.append(rec)
        offset += 20
        if offset >= total or not posts:
            break
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
    """JOIN (join.com): Die Firmenseite zeigt 5 Stellen je Seite (?page=N). Die öffentliche Liste
    /api/public/companies/<ID>/jobs gibt es seit November 2026 nicht mehr (404). Hinter der letzten echten Seite
    liefert JOIN die vorige noch einmal (pagination.page ≠ N) — dort endet das Blättern; pagination.total zählt die
    Initiativbewerbung mit. id = Nummer aus der Stellen-URL (wie im Feld seen der Tabelle); JOIN vergibt sie beim
    Auffrischen neu."""
    slug = e["slug"]
    base = f"https://join.com/companies/{slug}"
    first = join_page(base)
    if first is None:
        return None
    jl = first.get("jobs") or {}
    items = list(jl.get("items") or [])
    pag = jl.get("pagination") or {}
    total, pages = int(pag.get("total") or 0), int(pag.get("pageCount") or 1)
    complete = True
    for pno in range(2, min(pages, 40) + 1):
        time.sleep(0.6)
        d = join_page(f"{base}?page={pno}")
        if d is None:
            complete = False
            break
        jl = d.get("jobs") or {}
        if int((jl.get("pagination") or {}).get("page") or 0) != pno:
            break
        items.extend(jl.get("items") or [])
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
    if not complete or (total and len(ids) < total - 1):
        err(f"JOIN {e['name']}: Liste unvollständig ({len(ids)} von {total} gelesen)")
    return jobs


def scan_sitemap(e, details_cache):
    """Karriereseiten mit sitemap.xml und JSON-LD (JobPosting) auf der Stellenseite — z. B. EnBW und Netze BW (Phenom).
    Der Titel kommt zuerst grob aus der Adresse (für den Titel-Filter), Text und Datum von der Stellenseite
    (Cache "sm:<Adresse>", je Lauf höchstens max_details neue Seiten)."""
    def fetch(url):
        raw = http_get(url, headers=LI_HEADERS, as_json=False)
        return None if raw is None else (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)

    xml = fetch(e["sitemap"])
    if xml is None:
        return None
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
    if "<sitemapindex" in xml:
        locs = []
        for sm in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)[:10]:
            sub = fetch(html.unescape(sm))
            locs += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sub or "")
            time.sleep(0.3)
    rx = re.compile(e.get("job_regex") or r"/job/([^/]+)/([^/?#]+)")
    max_new, fetched, jobs = int(e.get("max_details", 80)), 0, []
    for url in dict.fromkeys(html.unescape(u) for u in locs):
        m = rx.search(url)
        if not m:
            continue
        gid = m.group("id") if "id" in rx.groupindex else m.group(1)
        gslug = m.group("slug") if "slug" in rx.groupindex else m.group(2)
        rough = re.sub(r"\s+", " ", re.sub(r"\b[wmdfx]( [wmdfx]){1,2}\b", " ", unquote(gslug).replace("-", " "))).strip()
        key = f"sm:{url}"
        det = details_cache.get(key)
        if det is None and e.get("details", True) and title_tier(rough) != "drop" and fetched < max_new:
            page = fetch(url)
            fetched += 1
            det = xing_details(page) if page else None
            if det:
                details_cache[key] = det
            time.sleep(0.7)
        det = det or {}
        if det.get("countries") and not any(c in GERMANY for c in det["countries"]):
            continue
        if e.get("need_country") and not det.get("countries"):
            continue
        title = det.get("title") or rough
        rec = {"id": f"sm-{e['id']}-{gid}", "source": "Сайт компании", "company": e["name"], "title": title,
               "location": det.get("location") or "", "published": det.get("published"), "url": url,
               "title_tier": title_tier(title), "found_by": [f"Компании под наблюдением: {e['name']}"]}
        if rec["title_tier"] == "prefer" and det.get("description"):
            rec["description"] = cut(det["description"])
        if e.get("pv_only"):
            rec["_text"] = det.get("description") or ""  # nur für den pv_only-Filter in scan_feeds
        jobs.append(rec)
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


# ----------------------------------------------------------------------------- eigene Karriereseiten und weitere Stellensysteme
# Seit 15.11.2026: Firmen ohne Personio/JOIN/Recruitee/… werden direkt von ihrer Karriereseite gelesen (vorher las die
# Morgenaufgabe sie per WebFetch oder sah sie gar nicht — nur über die Jobbörse nach Namen). robots.txt wird beachtet,
# Schutz gegen automatische Abrufe wird nicht umgangen.

ROBOTS = {}
PAGE_UA = {**UA, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}


def robots_ok(url):
    """robots.txt der Seite (Kennung «jobscan»): nicht lesbar → erlaubt; 401/403 → verboten."""
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in ROBOTS:
        rp = None
        try:
            r = requests.get(base + "/robots.txt", headers=PAGE_UA, timeout=20)
            if r.status_code in (401, 403):
                rp = False
            elif r.status_code == 200 and "<html" not in r.text[:300].lower():
                rp = robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
        except Exception:  # noqa: BLE001
            rp = None
        ROBOTS[base] = rp
    rp = ROBOTS[base]
    if rp is None:
        return True
    if rp is False:
        return False
    try:
        return rp.can_fetch("jobscan", url)
    except Exception:  # noqa: BLE001
        return True


def page_get(url, quiet=False, as_json=False):
    """Seite als Text (oder JSON); None, wenn robots.txt es verbietet oder die Seite nicht lädt."""
    if not robots_ok(url):
        if not quiet:
            err(f"robots.txt verbietet {url}")
        return None
    raw = http_get(url, headers=PAGE_UA, as_json=as_json) if not quiet else _get_quiet(url, as_json)
    if raw is None or as_json:
        return raw
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("cp1252", "replace")
    return raw


def _get_quiet(url, as_json):
    try:
        r = requests.get(url, headers=PAGE_UA, timeout=30)
        if r.status_code != 200:
            return None
        return r.json() if as_json else r.content
    except Exception:  # noqa: BLE001
        return None


A_TAG = re.compile(r"""<a\b((?:[^>"']|"[^"]*"|'[^']*')*)>(.*?)</a>""", re.S | re.I)
A_HREF = re.compile(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
A_TITLE = re.compile(r"""(?:title|aria-label)\s*=\s*(?:"([^"]+)"|'([^']+)')""", re.I)
NAV_TAIL = re.compile(r"\s*(?:mehr erfahren|mehr infos?|alle infos|jetzt bewerben|zur stelle|zum job|zur stellenanzeige|stelle ansehen|"
                      r"stelle anzeigen|details|mehr|weiterlesen|view job|read more|learn more|apply now|more|»|›|→)\s*$", re.I)
TAG_RE = re.compile(r"""<(?:[^>"']|"[^"]*"|'[^']*')*>""")
TITLE_PREFIX = re.compile(r"^(?:Bewirb (?:dich|Dich) (?:jetzt )?als|Bewerben Sie sich (?:jetzt )?als|Stellenanzeige\s*[-–:|]|Stellenangebot\s*[-–:|]|"
                          r"Job Opening\s*[-–:|]|Jobangebot\s*[-–:|])\s*", re.I)
# keine echte Stelle: Initiativbewerbung, allgemeiner Aufruf
NOT_A_JOB = re.compile(r"^\s*(?:Initiativ\w*|Spontaneous application|Open application|Unsolicited application)|Initiativbewerbung|"
                       r"Wir wachsen und suchen Dich|Initiativ bewerben", re.I)


def clean_title(t):
    t = re.sub(r"\s+", " ", html.unescape(t or "")).strip()
    return TITLE_PREFIX.sub("", t).strip(" -–|:")


GENDER_MARK = re.compile(r"\(\s*[mwdfxi](?:\s*[/|*,]\s*[mwdfxi]){1,3}\s*\)|\ball genders\b|\(gn\)|\b[mwd]/[mwd]/[mwd]\b|\b[mf]/[mf]/[dx]\b|\(alle\)", re.I)


def page_links(base, text):
    """(Adresse, Linktext, title-Attribut) aller Links einer Seite."""
    out = []
    for m in A_TAG.finditer(text or ""):
        h = A_HREF.search(m.group(1))
        if not h:
            continue
        href = html.unescape(next(g for g in h.groups() if g is not None)).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        label = re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", re.sub(r"<(script|style|svg)\b.*?</\1>", " ", m.group(2), flags=re.S | re.I)))).strip()
        t = A_TITLE.search(m.group(1))
        alt = html.unescape(next(g for g in t.groups() if g)) if t else ""
        out.append((urljoin(base, href).split("#")[0], label, alt))
    return out


def _nav_strip(s):
    s = (s or "").strip()
    while True:
        n = NAV_TAIL.sub("", s).strip()
        if n == s:
            return s
        s = n


def page_details(url, details_cache, pv_only=False):
    """Titel, Ort, Datum und Text einer Stellenseite (JSON-LD, sonst <main>/<article>), Cache "pg:<Adresse>".
    None, wenn die Seite nicht lesbar ist (dann Titel und Ort aus der Liste)."""
    key = f"pg:{url}"
    if key in details_cache:
        return details_cache[key]
    if re.search(r"\.pdf(?:$|\?)", url, re.I):
        return None
    t = page_get(url, quiet=True)
    if not t:
        return None
    det = xing_details(t) or {}
    if not det.get("description"):
        m = re.search(r"<main\b[^>]*>(.*?)</main>", t, re.S | re.I) or re.search(r"<article\b[^>]*>(.*?)</article>", t, re.S | re.I)
        b = None if m else re.search(r"<body\b[^>]*>(.*)</body>", t, re.S | re.I)
        body = m.group(1) if m else (b.group(1) if b else "")
        body = re.sub(r"<(script|style|nav|header|footer|form|svg|noscript)\b.*?</\1>", " ", body, flags=re.S | re.I)
        det["description"] = strip_html(body)
        det["text_from"] = "main" if m else ("body" if b else "none")
    if not det.get("title"):
        h1 = re.search(r"<h1\b[^>]*>(.*?)</h1>", t, re.S | re.I)
        ttl = re.search(r"<title[^>]*>(.*?)</title>", t, re.S | re.I)
        raw = strip_html(h1.group(1)) if h1 else (strip_html(ttl.group(1)) if ttl else "")
        det["title"] = re.split(r"\s+[|–-]\s+(?=[^|–-]*$)", raw)[0].strip() if raw and not h1 else raw
    det["cached_at"] = TODAY
    details_cache[key] = det
    time.sleep(0.6)
    return det


def scan_page(e, details_cache):
    """Eigene Karriereseite: Stellen-Links nach Muster e["link"] (Regex auf die volle Adresse, Gruppe «id» = Stellen-Nr.).
    Titel aus dem Linktext (e["title_regex"]: Gruppen title/loc/org), sonst von der Stellenseite; e["keep"]/e["drop"]:
    Regex auf den Linktext; e["need_marker"]: nur Links mit (m/w/d) o. ä.; e["paginate"]: {"url": "...{n}...", "start",
    "step", "max"}; e["text"] = false: Stellenseiten nicht öffnen; e["mode"] = "headings": Stellen stehen nur als
    Überschrift auf der Seite (ohne eigenen Link)."""
    link_rx = re.compile(e.get("link") or r"^$", re.I)
    title_rx = re.compile(e["title_regex"], re.I | re.S) if e.get("title_regex") else None
    keep_rx = re.compile(e["keep"], re.I) if e.get("keep") else None
    drop_rx = re.compile(e["drop"], re.I) if e.get("drop") else None
    urls = [e["url"]] + list(e.get("urls") or [])
    pg = e.get("paginate")
    if pg:
        urls += [pg["url"].format(n=pg.get("start", 0) + i * pg.get("step", 1)) for i in range(int(pg.get("max", 10)))]
    found, loaded = {}, 0
    for u in urls:
        t = page_get(u)
        if t is None:
            if pg and loaded:
                break
            continue
        loaded += 1
        before = len(found)
        if e.get("mode") == "headings":
            for m in re.finditer(r">([^<>]{6,140})<", t):
                txt = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()
                if not GENDER_MARK.search(txt) or re.search(r"\b(?:wir|suchen|unser|unsere|Sie|du|zum nächstmöglichen)\b", txt):
                    continue
                if title_rx:
                    mm = title_rx.search(txt)
                    if not mm:
                        continue
                    txt = (mm.groupdict().get("title") or mm.group(1)).strip()
                key = re.sub(r"[^a-z0-9]+", "-", txt.lower()).strip("-")[:80]
                found.setdefault(key, {"url": f"{u}#{key}", "label": txt, "alt": "", "key": key})
            continue
        for url, label, alt in page_links(u, t):
            m = link_rx.search(url)
            if not m:
                continue
            full = f"{label} {alt}".strip()
            if e.get("need_marker") and not GENDER_MARK.search(full):
                continue
            key = (m.groupdict().get("id") if "id" in link_rx.groupindex else None) or re.sub(r"^https?://(www\.)?", "", url).rstrip("/")
            cur = found.get(key)
            lab = _nav_strip(label)

            def good(x):
                return 4 <= len(x) <= 120
            if cur is None:
                found[key] = {"url": url, "label": lab, "alt": alt, "key": key}
            elif (GENDER_MARK.search(lab) and good(lab) and not GENDER_MARK.search(cur["label"])) or (good(lab) and not good(cur["label"])):
                cur.update({"label": lab, "alt": alt or cur["alt"]})
        if pg and len(found) == before and u != e["url"]:
            break  # Blättern: keine neuen Stellen mehr
        time.sleep(0.5)
    if not loaded:
        return None
    jobs, fetched, max_new = [], 0, int(e.get("max_details", 40))
    for key, f in found.items():
        label = f["label"] or f["alt"]
        title, loc, org = label, "", ""
        if title_rx and label and e.get("mode") != "headings":
            mm = title_rx.search(label)
            if mm:
                gd = mm.groupdict()
                title = (gd.get("title") or (mm.group(1) if mm.groups() else label) or "").strip()
                loc, org = (gd.get("loc") or "").strip(), (gd.get("org") or "").strip()
        if keep_rx and not keep_rx.search(label):
            continue
        if drop_rx and drop_rx.search(label):
            continue
        det = None
        need_title = not title or len(title) < 4
        want = need_title or e.get("title_from") == "page" or title_tier(title) != "drop"
        if e.get("text", True) and e.get("mode") != "headings" and want:
            cached = f"pg:{f['url']}" in details_cache
            if cached or fetched < max_new:
                det = page_details(f["url"], details_cache)
                fetched += 0 if cached else 1
        det = det or {}
        if need_title or e.get("title_from") == "page":
            title = det.get("title") or title
        title = clean_title(title)
        if not title or NOT_A_JOB.search(title):
            continue
        if det.get("countries") and not any(c in GERMANY for c in det["countries"]) and not e.get("any_country"):
            continue
        sid = key if len(str(key)) <= 40 else hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:12]
        rec = {"id": f"pg-{e['id']}-{sid}", "source": "Сайт компании", "company": e["name"], "title": title,
               "location": det.get("location") or loc or "", "published": det.get("published"), "url": f["url"],
               "title_tier": title_tier(title), "found_by": [f"Компании под наблюдением: {e['name']}"]}
        text = det.get("description") or ""
        if rec["title_tier"] == "prefer" and text:
            rec["description"] = cut(text)
        if e.get("pv_only"):
            rec["_text"] = text if det.get("text_from") not in ("none", "body") else ""
        jobs.append(rec)
    return jobs


def scan_dvinci(e):
    """d.vinci (<firma>.dvinci-hr.com oder eigene Domain): /jobPublication/list.json — alle Stellen mit Text.
    e["keep"]: Regex auf Titel und internen Namen der Ausschreibung (eine Börse für mehrere Firmen)."""
    host = e["host"]
    d = page_get(f"https://{host}/jobPublication/list.json", as_json=True)
    if not isinstance(d, list):
        return None
    keep_rx = re.compile(e["keep"], re.I) if e.get("keep") else None
    jobs = []
    for p in d:
        if not isinstance(p, dict):
            continue
        title = (p.get("position") or p.get("pageTitle") or "").strip()
        op = p.get("jobOpening") if isinstance(p.get("jobOpening"), dict) else {}
        if NOT_A_JOB.search(title):
            continue
        if keep_rx and not keep_rx.search(f"{title} {op.get('name') or ''}"):
            continue
        locs = []
        for lo in op.get("locations") or []:
            if isinstance(lo, dict):
                a = lo.get("address") if isinstance(lo.get("address"), dict) else {}
                locs.append(lo.get("name") or lo.get("city") or a.get("city") or "")
        text = strip_html(" \n".join(str(p.get(k) or "") for k in ("introduction", "tasks", "profile", "weOffer") if p.get(k)))
        rec = {"id": f"dvinci-{e['id']}-{p.get('id')}", "source": "Сайт компании", "company": e["name"], "title": title,
               "location": ", ".join(x for x in locs if x), "published": (p.get("startDate") or "")[:10] or None,
               "url": p.get("jobPublicationURL") or f"https://{host}/de/jobs/{p.get('id')}",
               "title_tier": title_tier(title), "found_by": [f"Компании под наблюдением: {e['name']}"]}
        if rec["title_tier"] == "prefer" and text:
            rec["description"] = cut(text)
        if e.get("pv_only"):
            rec["_text"] = text
        jobs.append(rec)
    return jobs


def scan_workable(e):
    """Workable (apply.workable.com/<konto>): Widget-Liste; Text für passende Titel aus der v2-Schnittstelle."""
    acc = e["account"]
    d = page_get(f"https://apply.workable.com/api/v1/widget/accounts/{acc}", as_json=True)
    if not isinstance(d, dict):
        return None
    crx = re.compile(e.get("country_regex") or r"Germany|Deutschland", re.I)
    jobs = []
    for j in d.get("jobs") or []:
        places = [j] + [x for x in (j.get("locations") or []) if isinstance(x, dict)]
        if not any(crx.search(str(x.get("country") or "")) for x in places):
            continue
        title = j.get("title") or ""
        rec = {"id": f"workable-{acc}-{j.get('shortcode')}", "source": "Сайт компании", "company": e["name"], "title": title,
               "location": ", ".join(x for x in (j.get("city"), j.get("country")) if x), "remote": bool(j.get("telecommuting")),
               "published": j.get("published_on") or j.get("created_at"), "url": j.get("url") or j.get("shortlink"),
               "title_tier": title_tier(title), "found_by": [f"Компании под наблюдением: {e['name']}"]}
        if rec["title_tier"] == "prefer":
            det = page_get(f"https://apply.workable.com/api/v2/accounts/{acc}/jobs/{j.get('shortcode')}", quiet=True, as_json=True)
            if isinstance(det, dict):
                rec["description"] = cut(strip_html(f"{det.get('description') or ''} {det.get('requirements') or ''}"))
            time.sleep(1.0)
        jobs.append(rec)
    return jobs


def scan_greenhouse(e):
    """Greenhouse (boards-api.greenhouse.io / boards-api.eu.greenhouse.io): Liste mit Text (content=true)."""
    api = e.get("api") or "boards-api.greenhouse.io"
    d = page_get(f"https://{api}/v1/boards/{e['board']}/jobs?content=true", as_json=True)
    if not isinstance(d, dict):
        return None
    lrx = re.compile(e.get("location_regex") or r"German|Deutschland|Berlin|Hamburg|München|Munich|Frankfurt|Köln|Stuttgart|Remote", re.I)
    jobs = []
    for j in d.get("jobs") or []:
        loc = ((j.get("location") or {}).get("name") or "") if isinstance(j.get("location"), dict) else ""
        if not lrx.search(loc):
            continue
        title = j.get("title") or ""
        rec = {"id": f"gh-{e['board']}-{j.get('id')}", "source": "Сайт компании", "company": e["name"], "title": title,
               "location": loc, "published": (j.get("updated_at") or "")[:10] or None, "url": j.get("absolute_url"),
               "title_tier": title_tier(title), "found_by": [f"Компании под наблюдением: {e['name']}"]}
        if rec["title_tier"] == "prefer":
            rec["description"] = cut(strip_html(html.unescape(j.get("content") or "")))
        jobs.append(rec)
    return jobs


def scan_rss(e):
    """RSS-Liste der Stellen (z. B. Avature: …/SearchJobs/feed/). e["keep"]: Regex auf den Titel."""
    t = page_get(e["url"])
    if t is None:
        return None
    try:
        root = ET.fromstring(t.encode("utf-8"))
    except Exception as ex:  # noqa: BLE001
        err(f"RSS {e['name']}: {ex}")
        return None
    keep_rx = re.compile(e["keep"], re.I) if e.get("keep") else None
    jobs = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title or NOT_A_JOB.search(title) or (keep_rx and not keep_rx.search(title)):
            continue
        m = re.search(e.get("id_regex") or r"jobId=(\d+)", link)
        text = strip_html(it.findtext("description") or "")
        pub = it.findtext("pubDate") or ""
        try:
            pub = dt.datetime.strptime(pub[:16].strip(), "%a, %d %b %Y").date().isoformat() if pub else None
        except Exception:  # noqa: BLE001
            pub = None
        rec = {"id": f"rss-{e['id']}-{m.group(1) if m else hashlib.sha1(link.encode('utf-8')).hexdigest()[:12]}", "source": "Сайт компании",
               "company": e["name"], "title": title, "location": "", "published": pub, "url": link,
               "title_tier": title_tier(title), "found_by": [f"Компании под наблюдением: {e['name']}"]}
        if rec["title_tier"] == "prefer" and len(text) > 200:
            rec["description"] = cut(text)
        if e.get("pv_only"):
            rec["_text"] = text
        jobs.append(rec)
    return jobs


def _jget(obj, path):
    for part in [p for p in (path or "").split(".") if p]:
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


def scan_jsonlist(e):
    """Stellenliste als JSON (eigene Schnittstelle der Firma, WordPress «job-listings»): e["items"] = Pfad zur Liste,
    e["fields"] = {"id", "title", "text", "date", "url"} (Pfade mit Punkt), e["url_template"] mit {id},
    e["text_regex"]: Text aus allen Feldern, deren Name passt."""
    d = page_get(e["url"], as_json=True)
    if d is None:
        return None
    items = _jget(d, e.get("items")) if e.get("items") else d
    if not isinstance(items, list):
        err(f"JSON {e['name']}: keine Liste unter «{e.get('items')}»")
        return None
    f = e.get("fields") or {}
    jobs = []
    for it in items:
        if not isinstance(it, dict):
            continue
        jid = _jget(it, f.get("id", "id"))
        title = strip_html(html.unescape(str(_jget(it, f.get("title", "title")) or "")))
        if not title or NOT_A_JOB.search(title):
            continue
        url = _jget(it, f["url"]) if f.get("url") else None
        url = url or (e.get("url_template") or "").format(id=jid)
        text = strip_html(html.unescape(str(_jget(it, f.get("text", "")) or ""))) if f.get("text") else ""
        if e.get("text_regex"):  # Text über mehrere Felder verteilt (z. B. umantis_*-Textblöcke)
            trx = re.compile(e["text_regex"])
            text = strip_html(html.unescape(" \n".join(str(v) for k, v in it.items() if trx.search(k) and isinstance(v, str) and v.strip())))
        rec = {"id": f"js-{e['id']}-{jid}", "source": "Сайт компании", "company": e["name"], "title": title, "location": "",
               "published": str(_jget(it, f.get("date", "")) or "")[:10] or None, "url": url,
               "title_tier": title_tier(title), "found_by": [f"Компании под наблюдением: {e['name']}"]}
        if rec["title_tier"] == "prefer" and text:
            rec["description"] = cut(text)
        if e.get("pv_only"):
            rec["_text"] = text
        jobs.append(rec)
    return jobs


def _title_key(t):
    t = re.sub(r"\((?:[mwfdx]\s*/\s*){1,3}[mwfdx]\)|\(all genders\)|\(alle\)", " ", (t or "").lower())
    return re.sub(r"[^0-9a-zäöüß]+", "", t)


def scan_feeds(details_cache):
    try:
        return _scan_feeds(details_cache)
    finally:
        FEEDS_DONE.set()  # scan_fallback wartet darauf


def _scan_feeds(details_cache):
    out = {}
    # JOIN zuerst: bei Firmen mit zwei Systemen (ju:niz: JOIN und Personio) bleiben die JOIN-ids (wie seen in der
    # Tabelle); vom zweiten System kommen nur Stellen mit neuem Titel dazu.
    kinds = (("join", lambda e: scan_join(e, details_cache)), ("ashby", scan_ashby), ("personio", scan_personio),
             ("recruitee", scan_recruitee), ("smartrecruiters", scan_smartrecruiters),
             ("workday", lambda e: scan_workday(e, details_cache)),
             ("sitemap", lambda e: scan_sitemap(e, details_cache)), ("dvinci", scan_dvinci), ("workable", scan_workable),
             ("greenhouse", scan_greenhouse), ("rss", scan_rss), ("jsonlist", scan_jsonlist),
             ("page", lambda e: scan_page(e, details_cache)))
    counts = {}
    later = []  # Workday-Firmen, die im ersten Durchgang gedrosselt wurden (429)

    def run(kind, fn, e):
        try:
            return fn(e)
        except Exception as ex:  # noqa: BLE001
            err(f"{kind} {e.get('name')}: {ex}")
            return None

    def take(kind, e, jobs):
        if kind == "page":
            STATS.setdefault("page_counts", {})[e["id"]] = None if jobs is None else len(jobs)
        if jobs and e.get("pv_only"):
            # Arbeitgeber ohne PV/BESS-Schwerpunkt (Konzern, Stadtwerk): nur Stellen mit PV/Speicher/Netzanschluss
            # im Titel oder mindestens zweimal im Text — sonst füllt der Standardtext der Firma die Liste
            jobs = [j for j in jobs if PV_ONLY.search(j.get("title") or "")
                    or len(PV_ONLY.findall(j.get("_text") or j.get("description") or "")) >= 2]
        for j in jobs or []:
            j.pop("_text", None)
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

    for kind, fn in kinds:
        for e in CFG.get(kind, []):
            if kind == "workday" and WD["off"]:
                later.append(e)
                continue
            jobs = run(kind, fn, e)
            if kind == "workday" and jobs is None and WD["status"] in ("blocked", "429"):
                later.append(e)
                continue
            take(kind, e, jobs)
            time.sleep(0.3)
    if later:
        # Workday hat abgewiesen: nach den übrigen Quellen (mindestens workday_retry_s nach der letzten Abweisung) noch
        # einmal; was dann nicht lädt, kommt in watch_failed (die Morgenaufgabe prüft die Firma selbst, falls sie kann)
        st = wd_stats()
        st["deferred"] = sorted({e["id"] for e in later})
        time.sleep(max(0.0, float(CFG.get("workday_retry_s", 150)) - (time.time() - WD["t429"])))
        WD.update(off=False, fails=0, patient=True)
        fn = dict(kinds)["workday"]
        for e in later:
            jobs = None if WD["off"] else run("workday", fn, e)
            if jobs is None:
                st["failed"].append(e["name"])
                st.setdefault("failed_ids", []).append(e["id"])
                err(f"workday {e['name']}: Liste nicht geladen ({WD['status']})")
            take("workday", e, jobs)
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
                note_drop(title, gx("author"), e["name"])
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


LI = {"why": {}, "t_fail": 0.0}  # Gründe abgewiesener LinkedIn-Seiten (stats) und Zeitpunkt der letzten Abweisung


def li_job_text(job_id):
    """Text der öffentlichen LinkedIn-Stellenseite (ohne Login); None, wenn nicht lesbar (Login-Seite, 429 …)."""
    why = None
    try:
        r = requests.get(f"https://www.linkedin.com/jobs/view/{job_id}", headers=LI_HEADERS, timeout=30)
        if r.status_code != 200:
            why = f"HTTP {r.status_code}"
        elif "/jobs/view/" not in r.url:
            why = "Anmeldeseite"
    except Exception as ex:  # noqa: BLE001
        why = type(ex).__name__
    if why is None:
        m = re.search(r'<div class="show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>', r.text, re.S)
        text = strip_html(m.group(1)) if m else ""
        if text:
            return text
        why = "kein Text auf der Seite"
    LI["why"][why] = LI["why"].get(why, 0) + 1
    LI["t_fail"] = time.time()
    return None


def li_id(url):
    m = re.search(r"/jobs/view/(?:[^/?#]*-)?(\d+)", url or "")
    return m.group(1) if m else None


def li_det():
    return {"read": 0, "cached": 0, "tries_failed": 0, "unreadable": 0, "no_pv_bess_dropped": 0, "retry_rounds": 0, "retry_read": 0}


def li_fill_texts(recs, details_cache, det, pace=(1.5, 3.5)):
    """LinkedIn gibt in der Suche keinen Text: Stellenseite lesen und Stellen ohne PV/BESS im Text mit _drop
    markieren (wie bei der Jobbörse; nicht bei Stellen mit _keep — Ersatz für die Liste einer beobachteten Firma). Gelesene Texte bleiben 7 Tage im details-Cache (Schlüssel "li:<id>"), damit ein
    zweiter Lauf am selben Tag (Push, Nachstart) LinkedIn nicht erneut abfragt. Titel mit Rollenwort (prefer) zuerst.
    LinkedIn weist einen Teil der Seiten mit 429 ab (25.11.2026: 12 von 33, nach zwei Minuten Pause alle lesbar): eine
    abgewiesene Seite bleibt offen, nach vier Abweisungen in Folge endet die Runde. Rückgabe: Stellen, deren Text noch
    fehlt — li_retry versucht sie später noch einmal."""
    todo = [r for r in recs if r.get("source") == "LinkedIn" and not r.get("description") and not r.get("_drop")]
    todo.sort(key=lambda r: r.get("title_tier") != "prefer")
    li_max = int((CFG.get("jobspy") or {}).get("linkedin_details_max", 150))
    pending, in_row = [], 0
    for rec in todo:
        jid = li_id(rec.get("url"))
        ck = f"li:{jid}" if jid else None
        hit = details_cache.get(ck) if (details_cache is not None and ck) else None
        if hit is not None:
            det["cached"] += 1
            text = hit.get("text") or ""
        else:
            if not jid:
                continue
            if det["read"] + det["tries_failed"] >= li_max or in_row >= 4:
                pending.append(rec)
                continue
            text = li_job_text(jid)
            if text is None:
                det["tries_failed"] += 1
                in_row += 1
                pending.append(rec)
                time.sleep(6)
                continue
            in_row = 0
            det["read"] += 1
            time.sleep(random.uniform(*pace))
        loose = bool(INDUSTRY_STRICT.search(f"{rec['title']} {rec['company']} {text}"))
        # regionaler Durchgang mit breiten Wörtern (_strict): eindeutiges PV-/Speicherwort verlangen (wie title_soft_proof)
        relevant = bool(PROOF.search(f"{rec['title']} {rec['company']} {text}")) if rec.get("_strict") else loose
        if hit is None and ck and details_cache is not None:
            details_cache[ck] = {"text": cut(text) if (loose or rec.get("_keep")) else "", "cached_at": TODAY}
        if not relevant and not rec.get("_keep"):
            det["no_pv_bess_dropped"] += 1
            rec["_drop"] = True
        elif text:
            rec["description"] = cut(text)
    return pending


def li_retry(pending, details_cache, det):
    """Spätere Leserunden für abgewiesene LinkedIn-Seiten: warten (pause_s seit der letzten Abweisung), dann langsamer
    noch einmal lesen — höchstens rounds Runden und max_s Sekunden (config jobspy.linkedin_retry); nicht mehr, wenn der
    ganze Lauf schon länger als 25 Minuten dauert. Rückgabe: Stellen, die auch danach ohne Text bleiben."""
    cfg = (CFG.get("jobspy") or {}).get("linkedin_retry") or {}
    rounds, pause, max_s = int(cfg.get("rounds", 3)), float(cfg.get("pause_s", 90)), float(cfg.get("max_s", 480))
    t0 = time.time()
    for _ in range(rounds):
        pending = [r for r in pending if not r.get("description") and not r.get("_drop")]
        wait = max(0.0, pause - (time.time() - LI["t_fail"]))
        if not pending or time.time() - t0 + wait > max_s or time.time() - START > 25 * 60:
            break
        time.sleep(wait)
        det["retry_rounds"] += 1
        before = det["read"]
        pending = li_fill_texts(pending, details_cache, det, pace=(3.0, 5.0))
        det["retry_read"] += det["read"] - before
    return [r for r in pending if not r.get("description") and not r.get("_drop")]


def scan_jobspy(details_cache=None):
    """LinkedIn über JobSpy (öffentliche Gastsuche): enge Begriffe (Rolle + Branche) bundesweit, dazu seit 25.11.2026 ein
    regionaler Durchgang mit breiten Branchenwörtern (config jobspy.regional: Stuttgart + Umkreis) — für Stellen bei
    Arbeitgebern in der Nähe, die kein Branchenwort im Titel tragen. Im regionalen Durchgang bleiben nur Titel mit
    Rollenwort (prefer) oder PV-/Speicherwort im Titel oder Firmennamen: die breite Suche bringt viel Beifang, und jede
    Stelle kostet eine Textabfrage. Im Text muss dann ein eindeutiges PV-/Speicherwort stehen (title_soft_proof) —
    «Erneuerbare» oder «Speicher» allein genügt bei der breiten Suche nicht."""
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
    reg = cfg.get("regional") or {}
    plan = [(site, term, None) for term in cfg.get("terms", []) for site in cfg.get("sites", ["indeed", "linkedin"])]
    plan += [("linkedin", term, reg) for term in reg.get("terms", [])]
    reg_st = {"searches": 0, "raw": 0, "kept": 0, "beifang": 0}
    for site, term, r in plan:
        tag = f"{site} {r.get('label', 'regional')}: {term}" if r else f"{site}: {term}"
        kw = dict(site_name=[site], search_term=term,
                  location=(r or {}).get("location") or (cfg.get("locations") or {}).get(site) or cfg.get("location", "Deutschland"),
                  country_indeed=cfg.get("country_indeed", "germany"), hours_old=int((r or cfg).get("hours_old", 24)),
                  results_wanted=int((r or cfg).get("results_wanted", 40)), linkedin_fetch_description=False,
                  description_format="markdown", verbose=0)
        if r and r.get("distance") is not None:
            kw["distance"] = int(r["distance"])  # Meilen
        if r:
            reg_st["searches"] += 1
        try:
            df = scrape_jobs(**kw)
        except Exception as ex:  # noqa: BLE001
            err(f"jobspy {tag}: {str(ex)[:120]}")
            continue
        if df is None or len(df) == 0:
            continue
        src = {"indeed": "Indeed", "linkedin": "LinkedIn"}.get(site, site)
        for _, row in df.iterrows():
            url = str(row.get("job_url") or "")
            if not url or url in out:
                if url in out and tag not in out[url]["found_by"]:
                    out[url]["found_by"].append(tag)
                continue
            loc = row.get("location")
            loc = "" if loc is None or isinstance(loc, float) else str(loc)
            if not in_germany(loc):
                abroad[site] = abroad.get(site, 0) + 1
                continue
            title, company = str(row.get("title") or ""), str(row.get("company") or "")
            tier = title_tier(title)
            if r:
                reg_st["raw"] += 1
            if tier == "drop":
                note_drop(title, company, src)
                continue
            if r and tier != "prefer" and not (CORE and CORE.search(f"{title} {company}")):
                reg_st["beifang"] += 1
                continue
            desc = row.get("description")
            desc = "" if desc is None or (isinstance(desc, float)) else str(desc)
            dp = row.get("date_posted")
            out[url] = {
                "id": f"{site}-{re.sub(r'[^0-9a-zA-Z]+', '-', url)[-50:]}",
                "source": src,
                "title": title,
                "company": company,
                "location": loc,
                "remote": bool(row.get("is_remote")) if row.get("is_remote") is not None else None,
                "published": str(dp)[:10] if dp is not None and str(dp) not in ("NaT", "nan", "None") else None,
                "url": url,
                "title_tier": tier,
                "description": cut(strip_html(desc)) if desc else None,
                "found_by": [tag],
            }
            if r:
                reg_st["kept"] += 1
                out[url]["_strict"] = True
        time.sleep(2)
    recs = list(out.values())
    det = li_det()
    for rec in recs:
        if rec["source"] == "Indeed" and rec.get("description") and not INDUSTRY_STRICT.search(f"{rec['title']} {rec['company']} {rec['description']}"):
            det["no_pv_bess_dropped"] += 1
            rec["_drop"] = True
    # erste Leserunde; was LinkedIn abweist, liest boards_jobspy nach den übrigen Suchen noch einmal (li_retry)
    LI["pending"] = li_fill_texts(recs, details_cache, det)
    LI["det"] = det
    STATS["jobspy"] = {"abroad_dropped": abroad, "regional": reg_st}
    return recs


FALLBACK = {}  # Firmen-id → {"jobs": [...], "via": [...]}: Ersatz für Firmenlisten, die nicht luden (scan_fallback)
FEEDS_DONE = threading.Event()  # scan_feeds ist fertig (STATS["workday"] steht)


def scan_fallback(details_cache):
    """Ersatz für Workday-Listen, die nicht luden (seit 16.11.2026 weist Cloudflare den GitHub-Server ab — das umgehen
    wir nicht): die Firma nach ihrem Namen auf Indeed und LinkedIn suchen (öffentliche Suche, letzte hours_old Stunden)
    und nur Treffer behalten, deren Firmenname passt. Das ist nicht die vollständige Liste der Firma, sondern was sie in
    den letzten Tagen dort ausgeschrieben hat — für neue Stellen reicht das. Suchname und Namensmuster: config workday[].fallback
    {query, match (Regex)}, sonst wie in employers (query, match). Ergebnis in FALLBACK; main() hängt die Stellen an die
    Firma in watch (Kennzeichen via)."""
    cfg = CFG.get("fallback") or {}
    if not cfg.get("enabled", True):
        return []
    FEEDS_DONE.wait(timeout=600)
    failed = list(dict.fromkeys((STATS.get("workday") or {}).get("failed_ids") or []))
    if not failed:
        return []
    try:
        from jobspy import scrape_jobs  # type: ignore
    except Exception as ex:  # noqa: BLE001
        err(f"jobspy nicht verfügbar: {ex}")
        return []
    by_name = {e["id"]: e for e in CFG.get("employers", [])}
    st = {}
    li_recs = []
    for cid in failed:
        ents = [e for e in CFG.get("workday", []) if e["id"] == cid]
        fbc = next((e["fallback"] for e in ents if e.get("fallback")), None) or {}
        emp = by_name.get(cid) or {}
        query = fbc.get("query") or emp.get("query")
        if not query:
            continue
        rx = re.compile(fbc.get("match") or re.escape((emp.get("match") or query).lower()), re.I)
        name = ents[0]["name"] if ents else cid
        pv_only = any(e.get("pv_only") for e in ents)
        jobs, c, ok = {}, {"indeed": 0, "linkedin": 0, "kept": 0}, False
        for site in cfg.get("sites", ["indeed", "linkedin"]):
            df = None
            for attempt in (1, 2):  # LinkedIn antwortet auf eine Suche manchmal leer: einmal wiederholen
                try:
                    df = scrape_jobs(site_name=[site], search_term=query, location="Germany" if site == "linkedin" else "Deutschland",
                                     country_indeed="germany", hours_old=int(cfg.get("hours_old", 168)),
                                     results_wanted=int(cfg.get("results_wanted", 30)), linkedin_fetch_description=False,
                                     description_format="markdown", verbose=0)
                    ok = True
                except Exception as ex:  # noqa: BLE001
                    df = None
                    if attempt == 2:
                        err(f"Ersatzsuche {site} «{query}»: {str(ex)[:120]}")
                if (df is not None and len(df)) or site != "linkedin" or attempt == 2:
                    break
                time.sleep(15)
            for _, row in (df.iterrows() if df is not None and len(df) else []):
                url, company = str(row.get("job_url") or ""), str(row.get("company") or "")
                loc = row.get("location")
                loc = "" if loc is None or isinstance(loc, float) else str(loc)
                if not url or url in jobs or not rx.search(company) or not in_germany(loc):
                    continue
                c[site] += 1
                title = str(row.get("title") or "")
                desc = row.get("description")
                desc = "" if desc is None or isinstance(desc, float) else strip_html(str(desc))
                dp = row.get("date_posted")
                jid = li_id(url) if site == "linkedin" else ((re.search(r"jk=([0-9a-f]+)", url) or [None, None])[1])
                jobs[url] = {"id": f"{'li' if site == 'linkedin' else 'indeed'}-{jid or hashlib.sha1(url.encode('utf-8')).hexdigest()[:12]}",
                             "source": "LinkedIn" if site == "linkedin" else "Indeed", "company": company, "title": title, "location": loc,
                             "published": str(dp)[:10] if dp is not None and str(dp) not in ("NaT", "nan", "None") else None,
                             "url": url, "title_tier": title_tier(title), "via": site,
                             "found_by": [f"Компании под наблюдением: {name} (поиск по имени: {'LinkedIn' if site == 'linkedin' else 'Indeed'})"]}
                if desc:
                    jobs[url]["_text"] = desc
            time.sleep(2)
        # dieselbe Stelle auf beiden Seiten: einmal (Indeed zuerst — dort kommt der Text mit)
        uniq, have = [], set()
        for j in jobs.values():
            k = _title_key(j["title"])
            if k not in have:
                have.add(k)
                uniq.append(j)
        FALLBACK[cid] = {"jobs": uniq, "ok": ok, "pv_only": pv_only, "via": [s for s in ("indeed", "linkedin") if c[s]]}
        st[cid] = c
        li_recs += [j for j in uniq if j["source"] == "LinkedIn" and j["title_tier"] == "prefer"]
    # LinkedIn-Texte nur für Titel mit Rollenwort (wie bei den Firmenlisten); _keep: nicht nach PV/BESS im Text aussortieren
    for j in li_recs:
        j["_keep"] = True
    det = LI.setdefault("det", li_det())
    LI["pending"] = (LI.get("pending") or []) + li_fill_texts(li_recs, details_cache, det)
    STATS["fallback"] = {"searched": st}
    log(f"Ersatzsuche nach Firmenname: {st}")
    return []


def fallback_jobs(cid):
    """Stellen aus scan_fallback für eine Firma, fertig für watch: pv_only wie bei den Firmenlisten (PV/Speicher im
    Titel oder mindestens zweimal im Text), Text nur bei Titeln mit Rollenwort."""
    fb = FALLBACK.get(cid) or {}
    out = []
    for j in fb.get("jobs") or []:
        text = j.pop("_text", None) or j.get("description") or ""
        j.pop("_keep", None)
        j.pop("_drop", None)
        if fb.get("pv_only") and not (PV_ONLY.search(j.get("title") or "") or len(PV_ONLY.findall(text)) >= 2):
            continue
        j.pop("description", None)
        if j["title_tier"] == "prefer" and text:
            j["description"] = cut(text)
        out.append(j)
    return out


def boards_jobspy(details_cache):
    """Indeed, LinkedIn und der Ersatz für ausgefallene Firmenlisten — nacheinander in einem Thread (ein Takt gegenüber
    LinkedIn); zum Schluss die späteren Leserunden für LinkedIn-Texte."""
    indeed = safe(scan_indeed, details_cache)
    li = safe(scan_jobspy, details_cache)
    safe(scan_fallback, details_cache)
    det = LI.get("det") or li_det()
    left = li_retry(LI.get("pending") or [], details_cache, det)
    li = [r for r in li if not r.pop("_drop", None)]
    for r in li:
        r.pop("_strict", None)
    det["unreadable"] = len(left)
    kept = {}
    for rec in li:
        kept[rec["source"]] = kept.get(rec["source"], 0) + 1
    STATS.setdefault("jobspy", {}).update({"kept": kept, "texts": det, "not_read_why": dict(LI["why"])})
    log(f"JobSpy: {len(li)} Treffer {kept}, Texte: {det}, abgewiesen: {LI['why']}")
    return indeed + li



def scan_indeed(details_cache=None):
    """Indeed über JobSpy: enge Begriffe (Rolle + Branche) und breite Branchenwörter wie in den Indeed-E-Mail-Alerts —
    so kommen auch Stellen ohne «Photovoltaik» im Titel. Indeed liefert den Text mit: Titel-Filter und PV/BESS-Prüfung
    wie überall. Gleichlautende Stellen einer Firma an vielen Orten bleiben einmal stehen (also = weitere Orte)."""
    cfg = CFG.get("indeed") or {}
    if not cfg.get("enabled", True):
        return []
    try:
        from jobspy import scrape_jobs  # type: ignore
    except Exception as ex:  # noqa: BLE001
        err(f"jobspy nicht verfügbar: {ex}")
        return []
    st = {"searches": 0, "failed": 0, "raw": 0, "abroad_dropped": 0, "title_dropped": 0, "no_pv_bess_dropped": 0, "collapsed": 0, "kept": 0}
    plan = [(t, int(cfg.get("results_wanted", 40)), None) for t in (cfg.get("terms") or (CFG.get("jobspy") or {}).get("terms", []))]
    plan += [(t, int(cfg.get("results_wanted_broad", 200)), None) for t in cfg.get("broad_terms", [])]
    # regionaler Durchgang (seit 25.11.2026): breite Branchenwörter um Stuttgart, längeres Fenster — die bundesweite
    # Suche zeigt nicht jede Stelle aus der Nähe (Probe: «Sachbearbeiter Photovoltaik», Stadtwerke Schorndorf, fehlte)
    reg = cfg.get("regional") or {}
    plan += [(t, int(reg.get("results_wanted", 100)), reg) for t in reg.get("terms", [])]
    st["regional"] = {"searches": 0, "raw": 0, "kept": 0}
    out, gone = {}, set()
    for term, want, r in plan:
        st["searches"] += 1
        tag = f"indeed {r.get('label', 'regional')}: {term}" if r else f"indeed: {term}"
        kw = dict(site_name=["indeed"], search_term=term, location=(r or cfg).get("location", "Deutschland"),
                  country_indeed=cfg.get("country", "germany"), hours_old=int((r or cfg).get("hours_old", 48)),
                  results_wanted=want, description_format="markdown", verbose=0)
        if r and r.get("distance") is not None:
            kw["distance"] = int(r["distance"])  # Meilen
        if r:
            st["regional"]["searches"] += 1
        try:
            df = scrape_jobs(**kw)
        except Exception as ex:  # noqa: BLE001
            st["failed"] += 1
            err(f"jobspy {tag}: {str(ex)[:120]}")
            time.sleep(3)
            continue
        for _, row in (df.iterrows() if df is not None and len(df) else []):
            url = str(row.get("job_url") or "")
            if not url or url in gone:
                continue
            if url in out:
                if tag not in out[url]["found_by"]:
                    out[url]["found_by"].append(tag)
                continue
            st["raw"] += 1
            if r:
                st["regional"]["raw"] += 1
            loc = row.get("location")
            loc = "" if loc is None or isinstance(loc, float) else str(loc)
            title, company = str(row.get("title") or ""), str(row.get("company") or "")
            if not in_germany(loc):
                st["abroad_dropped"] += 1
                gone.add(url)
                continue
            tier = title_tier(title)
            if tier == "drop":
                st["title_dropped"] += 1
                gone.add(url)
                note_drop(title, company, "Indeed")
                continue
            desc = row.get("description")
            desc = "" if desc is None or isinstance(desc, float) else str(desc)
            if desc and not (PROOF if r else INDUSTRY_STRICT).search(f"{title} {company} {desc}"):  # regional: eindeutiges Wort
                st["no_pv_bess_dropped"] += 1
                gone.add(url)
                continue
            dp = row.get("date_posted")
            out[url] = {
                "id": f"indeed-{re.sub(r'[^0-9a-zA-Z]+', '-', url)[-50:]}",
                "source": "Indeed",
                "title": title,
                "company": company,
                "location": loc,
                "remote": bool(row.get("is_remote")) if row.get("is_remote") is not None else None,
                "published": str(dp)[:10] if dp is not None and str(dp) not in ("NaT", "nan", "None") else None,
                "url": url,
                "title_tier": tier,
                "description": cut(strip_html(desc)) if desc else None,
                "found_by": [tag],
            }
            if r:
                st["regional"]["kept"] += 1
        time.sleep(1.5)
    kept, groups = [], {}
    for rec in out.values():
        k = (rec["company"].lower(), re.sub(r"\s+in\s+[A-ZÄÖÜ][\w .()/-]*$", "", rec["title"]).lower()[:60])
        if k in groups:
            groups[k]["also"] = groups[k].get("also", 0) + 1
            st["collapsed"] += 1
        else:
            groups[k] = rec
            kept.append(rec)
    st["kept"] = len(kept)
    STATS["indeed"] = st
    log(f"Indeed: {st}")
    return kept


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
    # Die Suche ohne Datumsfilter zeigt Tag für Tag dieselben Stellen (Reihenfolge nach Relevanz). Darum:
    # «letzte 24 Stunden» für enge Begriffe und breite Branchenwörter (bis pages_24h Seiten), dazu als Netz
    # «letzte Woche» für die engen Begriffe. sincePeriod ist der Datumsfilter der XING-Suche (Probe 14.11.2026).
    plan = [(t, "LAST_24_HOURS", int(cfg.get("pages_24h", 3)), None) for t in list(terms) + list(cfg.get("broad_terms") or [])]
    plan += [(t, "LAST_WEEK", 1, None) for t in terms]
    # regionaler Durchgang (seit 25.11.2026): breite Branchenwörter um Stuttgart (location + radius in km, Probe 25.11.:
    # «Photovoltaik», Stuttgart, 70 km, letzte Woche → 24 Stellen) — die bundesweite Suche zeigt je Begriff nur die
    # ersten Seiten nach Relevanz
    reg = cfg.get("regional") or {}
    plan += [(t, reg.get("since", "LAST_WEEK"), int(reg.get("pages", 2)), reg) for t in reg.get("terms", [])]
    st["regional"] = {"searches": 0, "cards": 0}
    for term, since, max_pages, r in plan:
        if fails >= 3:
            err("XING: Suche dreimal hintereinander ohne Antwort — abgebrochen")
            break
        tag = f"xing {r.get('label', 'regional')}: {term}" if r else f"xing: {term}"
        for pno in range(1, max_pages + 1):
            q = {"keywords": term, "location": (r or cfg).get("location", "Deutschland"), "sincePeriod": since}
            if r and r.get("radius"):
                q["radius"] = int(r["radius"])
            if r:
                st["regional"]["searches"] += 1
            if pno > 1:
                q["page"] = pno
            page = xing_get("https://www.xing.com/jobs/search?" + urlencode(q))
            st["searches"] += 1
            if not page:
                st["search_failed"] += 1
                fails += 1
                time.sleep(5)
                break
            fails = 0
            cards, new = xing_cards(page), 0
            for c in cards:
                st["cards"] += 1
                if r:
                    st["regional"]["cards"] += 1
                if c["id"] in found:
                    if tag not in found[c["id"]]["tags"]:
                        found[c["id"]]["tags"].append(tag)
                else:
                    found[c["id"]] = {**c, "tags": [tag]}
                    new += 1
                if not r:
                    found[c["id"]]["national"] = True
            time.sleep(random.uniform(2, 4))
            if len(cards) < 4 or not new:
                break
    st["unique"] = len(found)
    out, in_row = [], 0
    for xid, c in found.items():
        tier = title_tier(c["title"])
        if tier == "drop":
            st["title_dropped"] += 1
            note_drop(c["title"], c["company"], "XING")
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
        # nur im regionalen Durchgang (breite Wörter) gefunden: eindeutiges PV-/Speicherwort verlangen
        if det.get("description") and not (INDUSTRY_STRICT if c.get("national") else PROOF).search(f"{c['title']} {company} {det['description']}"):
            st["no_pv_bess_dropped"] += 1
            continue
        out.append({"id": f"xing-{xid}", "source": "XING", "title": det.get("title") or c["title"], "company": company,
                    "location": det.get("location") or c["location"], "published": det.get("published"),
                    "url": "https://www.xing.com" + c["path"], "title_tier": tier,
                    "description": cut(det["description"]) if det.get("description") else None,
                    "found_by": list(c["tags"])})
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
            score = -0.5 if is_stale(link) else 0
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


def serp_doc_id(j):
    """Googles Kennung der Anzeige (htidocid aus share_link, sonst aus der job_id): bleibt über Tage und Suchanfragen
    gleich. Die job_id von SerpApi hängt an der Suchanfrage und ändert sich jeden Tag (16./17.11.2026: dieselbe
    Anzeige mit zwei job_id) — als Schlüssel machte sie jede Google-Jobs-Stelle täglich «neu» (first_seen)."""
    for lk in (j.get("share_link"), j.get("share_url")):
        if lk and "htidocid=" in str(lk):
            v = (parse_qs(urlparse(str(lk)).query).get("htidocid") or [None])[0]
            if v:
                return v
    jid = str(j.get("job_id") or "")
    if jid:
        for dec in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                v = json.loads(dec(jid + "=" * (-len(jid) % 4)).decode("utf-8")).get("htidocid")
                if v:
                    return v
            except Exception:  # noqa: BLE001
                pass
    return None


def board_key(b):
    """Schlüssel einer Board-Stelle für first_seen: id; bei Google Jobs aus der Kennung der Anzeige (Dateien bis 17.11.2026
    tragen noch die anfrageabhängige id)."""
    i = str(b.get("id") or b.get("url"))
    if i.startswith("serp-"):
        doc = serp_doc_id(b)
        if doc:
            return "serp-" + hashlib.sha1(doc.encode("utf-8")).hexdigest()[:16]
    return i


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
        # nur Zahlen zum Guthaben (Monatskontingent, Rest, Zusatzguthaben, Verbrauch) — nie Schlüssel oder Adresse
        st["account"] = {k: acct[k] for k in ("searches_per_month", "plan_searches_left", "extra_credits", "this_month_usage")
                         if isinstance(acct.get(k), (int, float)) and not isinstance(acct.get(k), bool)}
    reserve = int(cfg.get("reserve", 10))
    if left is not None and int(left) <= reserve:
        err(f"SerpApi: nur noch {left} Suchen übrig – übersprungen")
        state.update({"last_date": TODAY, "used_today": 0, "left": left})
        save_json("serp-log.json", state)
        return []
    # Suchläufe: bundesweit und lokal (Stuttgart + Umkreis), Abfragen rotieren tageweise;
    # solange das Guthaben über boost_above liegt, doppelt so viele pro Tag; boost_above = 31 Tage × per_day + Reserve,
    # damit auch ohne Aufstockung ein voller Monat im Normaltakt gedeckt bleibt
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
    per_q = {}  # je Abfrage: Rohtreffer der ersten Seite, gibt es weitere Seiten, behalten, neu (neu zählt main())
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
        per_q[tag] = {"raw": 0, "more": False, "kept": 0, "new": 0}
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
        per_q[tag] = {"raw": len(jobs), "more": bool((d.get("serpapi_pagination") or {}).get("next_page_token")), "kept": 0, "new": 0}
        old = 0
        for j in jobs:
            if not isinstance(j, dict):
                continue
            title = (j.get("title") or "").strip()
            jid = serp_doc_id(j) or j.get("job_id") or f"{title}|{j.get('company_name')}"
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
                note_drop(title, j.get("company_name"), "Google Jobs")
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
    for rec in out.values():
        for tag in rec["found_by"]:
            if tag in per_q:
                per_q[tag]["kept"] += 1
    st.update({"used_today": used, "results": len(out), "queries": todays, "uds": bool(uds), "boost": boost, "per_query": per_q})
    state.update({"last_date": TODAY, "used_today": used, "left": (int(left) - used) if left is not None else None,
                  "month": TODAY[:7], "used_month": (state.get("used_month", 0) if state.get("month") == TODAY[:7] else 0) + used})
    save_json("serp-log.json", state)
    save_json("serp-results.json", {"date": TODAY, "items": list(out.values())})
    log(f"SerpApi: {used} Suchen, {len(out)} Treffer, übrig ≈ {state.get('left')}")
    return list(out.values())


# ----------------------------------------------------------------------------- main

def run_parallel(tasks):
    """Quellen nebeneinander abfragen: jede hat ihren eigenen Server und ihre eigenen Pausen.
    Fällt eine Quelle mit einem Fehler aus, läuft der Rest weiter (Ergebnis None)."""
    res, took = {}, {}

    def wrap(item):
        name, fn = item
        t = time.time()
        try:
            return name, fn(), None, round((time.time() - t) / 60, 1)
        except Exception as ex:  # noqa: BLE001
            return name, None, ex, round((time.time() - t) / 60, 1)

    with cf.ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        for name, val, exc, mins in ex.map(wrap, list(tasks.items())):
            took[name] = mins
            if exc is not None:
                err(f"Quelle {name}: abgebrochen: {exc}")
            res[name] = val
    STATS["minutes_by_source"] = took
    return res


def safe(fn, *a):
    try:
        return fn(*a) or []
    except Exception as ex:  # noqa: BLE001
        err(f"{fn.__name__}: abgebrochen: {ex}")
        return []


def write_dropped():
    """data/dropped.json: alle nach Titel verworfenen Stellen des Laufs; data/disputed.json: die strittigen der
    letzten 14 Tage (Rollenwort im Titel, PV/Speicher im Titel oder Firma mit eigenem Feed) für die Wochenübersicht."""
    seen_k, uniq = set(), []
    for d in list(DROPPED):
        k = (_title_key(d["t"]), (d["c"] or "").lower())
        if k not in seen_k:
            seen_k.add(k)
            uniq.append(d)
    by = {}
    for d in uniq:
        for w in d["w"]:
            by[w] = by.get(w, 0) + 1
    disputed = [d for d in uniq if (d["role"] or d["soft"]) and (d["s"] == "Лента компании" or (CORE and CORE.search(f"{d['t']} {d['c']}")))]
    save_json("dropped.json", {"date": TODAY, "total": len(uniq), "by_word": dict(sorted(by.items(), key=lambda x: -x[1])),
                               "disputed": disputed, "all": uniq})
    roll = load_json("disputed.json", {})
    cutoff = (NOW - dt.timedelta(days=14)).date().isoformat()
    for d in disputed:
        k = f"{_title_key(d['t'])}|{(d['c'] or '').lower()}"
        cur = roll.get(k) or {"t": d["t"], "c": d["c"], "s": d["s"], "w": d["w"], "first": TODAY, "days": 0}
        if cur.get("last") != TODAY:
            cur["days"] = int(cur.get("days") or 0) + 1
        cur["last"] = TODAY
        cur["soft"] = bool(d["soft"])
        roll[k] = cur
    save_json("disputed.json", {k: v for k, v in roll.items() if (v.get("last") or "") >= cutoff})
    STATS["dropped"] = {"total": len(uniq), "disputed": len(disputed), "soft": sum(1 for d in disputed if d["soft"])}


def health(prefer, boards, minutes):
    """Zähler je Quelle (data/history.json, 21 Tage) und Warnungen für die Zusammenfassung der Morgenaufgabe:
    Quelle ausgefallen, unvollständig oder deutlich unter ihrem üblichen Wert."""
    counts = {"Jobbörse": int(STATS.get("jobboerse_unique") or 0), "кандидаты Jobbörse": len(prefer)}
    for k, v in ((STATS.get("feeds") or {}).get("jobs") or {}).items():
        counts[f"ленты {k}"] = int(v)
    for src in ("LinkedIn", "Indeed", "XING", "Google Jobs", "eejobs.de", "greenjobs.de"):
        counts[src] = 0
    for b in boards:
        counts[b.get("source")] = counts.get(b.get("source"), 0) + 1
    hist = [h for h in load_json("history.json", []) if isinstance(h, dict)]
    prev = [h for h in hist if h.get("date") != TODAY][-14:]
    alerts = []
    if not counts["Jobbörse"]:
        alerts.append("Jobbörse не ответила")
    for e in ERRORS:
        m = re.match(r"(?:Quelle )?(\S+): abgebrochen", e)
        if m:
            alerts.append(f"источник {m.group(1).replace('scan_', '')} прервался с ошибкой")
    join_bad = sum(1 for e in ERRORS if e.startswith("JOIN") and "unvollständig" in e)
    if join_bad >= 3:
        alerts.append(f"JOIN: список вакансий неполный у {join_bad} из {len(CFG.get('join', []))} компаний")
    if STATS.get("pages_went_empty"):
        alerts.append("страницы вакансий вдруг пусты (проверить разметку): " + ", ".join(STATS["pages_went_empty"][:6]))
    wd_ids = set((STATS.get("workday") or {}).get("failed_ids") or [])  # eigene Warnung weiter unten
    failed = [f for f in (STATS.get("feeds") or {}).get("failed") or [] if f not in wd_ids]
    if failed:
        alerts.append("ленты компаний не загрузились: " + ", ".join(failed[:6]) + (f" и ещё {len(failed) - 6}" if len(failed) > 6 else ""))
    wd = STATS.get("workday") or {}
    if wd.get("failed"):
        # Seit 25.11.2026 sucht scan_fallback diese Firmen nach ihrem Namen auf Indeed und LinkedIn: gewarnt wird nur
        # noch, wenn auch das nicht lief (stats.workday und stats.fallback stehen in jedem Fall in der Datei)
        fb = STATS.get("fallback") or {}
        searched = set(fb.get("searched") or {}) - set(fb.get("no_search") or [])
        rest = [n for n, c in zip(wd["failed"], wd.get("failed_ids") or []) if c not in searched]
        if rest or not wd.get("failed_ids"):
            why = "защита Cloudflare от ботов" if wd.get("blocked") else "ошибка или лимит запросов"
            alerts.append(f"Workday не отдал списки ({why}), и поиск по имени компании не сработал: "
                          f"{', '.join(dict.fromkeys(rest or wd['failed']))} — их вакансии сегодня только из Jobbörse и досок")
    hosts = {}
    for e in ERRORS:
        m = re.match(r"(?:GET|POST) https?://([^/ ]+)", e)
        if m:
            hosts[m.group(1)] = hosts.get(m.group(1), 0) + 1
    if hosts:
        top = sorted(hosts.items(), key=lambda x: -x[1])
        alerts.append("не отвечают: " + ", ".join(f"{h} ({n})" for h, n in top[:8])
                      + (f" и ещё {len(top) - 8}" if len(top) > 8 else ""))
    for src, n in counts.items():
        vals = sorted(int((h.get("counts") or {}).get(src) or 0) for h in prev if src in (h.get("counts") or {}))
        if len(vals) < 4:
            continue
        med = vals[len(vals) // 2]
        if (med >= 3 and n == 0) or (med >= 8 and n < 0.4 * med):
            alerts.append(f"{src}: {n} (обычно около {med})")
    left = (STATS.get("serpapi") or {}).get("left")
    if isinstance(left, int) and left < 60:
        alerts.append(f"SerpApi: осталось {left} поисков")
    if minutes > 45:
        alerts.append(f"прогон длился {minutes} мин (лимит 60)")
    hist = [h for h in hist if h.get("date") != TODAY] + [{"date": TODAY, "generated": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                            "minutes": minutes, "counts": counts, "errors": len(ERRORS), "alerts": len(alerts)}]
    save_json("history.json", hist[-21:])
    return alerts


def main():
    details_cache = load_json("details.json", {})
    seen = load_json("seen.json", {"refnr": {}})

    def jobboerse():
        prefer, other, _ = scan_jobboerse(details_cache)
        return prefer, other, scan_employers(int(CFG.get("employer_days", 30)), details_cache)

    res = run_parallel({
        "jobboerse": jobboerse,
        "feeds": lambda: scan_feeds(details_cache),
        "atom_xing": lambda: safe(scan_atom) + safe(scan_xing, details_cache),
        "jobspy": lambda: boards_jobspy(details_cache),
        "serpapi": scan_serpapi,
    })
    if res.get("jobboerse") is None:
        raise RuntimeError("Jobbörse-Suche abgebrochen")
    prefer, other, watch = res["jobboerse"]
    for cid, entry in (res.get("feeds") or {}).items():
        cur = watch.get(cid)
        if cur:
            cur["jobs"].extend(entry.get("jobs", []))
            cur["method"] = f"{cur['method']}+{entry['method']}"
            if entry.get("error"):
                cur["error"] = entry["error"]
        else:
            watch[cid] = entry
    # Ersatz für Firmenlisten, die nicht luden (scan_fallback): Stellen aus der Suche nach dem Firmennamen anhängen.
    # Lief die Suche, gilt die Firma als geprüft (sie bleibt in watch) — die Morgenaufgabe könnte die Liste auch nicht öffnen.
    fb_new = {}
    for cid in list(FALLBACK):
        entry = watch.get(cid)
        if entry is None:
            continue
        have = {_title_key(j.get("title")) for j in entry.get("jobs") or []}
        new = [j for j in fallback_jobs(cid) if _title_key(j.get("title")) not in have]
        entry.setdefault("jobs", []).extend(new)
        fb_new[cid] = len(new)
        if FALLBACK[cid].get("ok"):
            entry["method"] = f"{entry['method']}→" + "+".join(FALLBACK[cid].get("via") or ["suche"])
            entry["fallback"] = True
            entry.pop("error", None)
    if FALLBACK:
        STATS.setdefault("fallback", {}).update({"added": fb_new, "no_search": sorted(c for c, v in FALLBACK.items() if not v.get("ok"))})
    boards = (res.get("atom_xing") or []) + (res.get("jobspy") or []) + (res.get("serpapi") or [])

    # Titel mit «weichem» Ausschlusswort (soft_only): nur mit PV-/Speicherbezug behalten, sonst wie bisher verwerfen
    soft_st = {"feed": 0, "title": 0, "text": 0, "no_pv_dropped": 0}

    def soft_gate(rec, source, company=None, own_feed=False):
        how = soft_keep(rec, company, own_feed)
        if how is None:
            return True
        if not how:
            soft_st["no_pv_dropped"] += 1
            note_drop(rec.get("title"), company or rec.get("company"), source, "ohne pv/speicher")
            return False
        soft_st[how] += 1
        rec["title_soft"] = True
        return True

    prefer = [r for r in prefer if soft_gate(r, "Jobbörse")]
    other = [r for r in other if soft_gate(r, "Jobbörse")]
    boards = [b for b in boards if soft_gate(b, b.get("source") or "доска")]

    # beobachtete Firmen: ausgeschlossene Titel (Monteure, Vertrieb, IT …) nicht in die Datei
    for entry in watch.values():
        allj = entry.get("jobs") or []
        entry["jobs_total"] = len(allj)
        for j in allj:
            by_name = bool(j.get("refnr"))  # Jobbörse nach Name (Teiltreffer im Firmennamen möglich) — sonst eigene Liste
            src = "Jobbörse по имени" if by_name else "Лента компании"
            if j.get("title_tier") == "drop":
                note_drop(j.get("title"), entry.get("name"), src)
            elif not soft_gate(j, src, entry.get("name"), own_feed=not by_name):
                j["title_tier"] = "drop"
        entry["jobs"] = [j for j in allj if j.get("title_tier") != "drop"]
    STATS["title_soft"] = soft_st
    # Karriereseiten (eigene Seite der Firma): bisher Stellen, heute keine einzige → vermutlich neue Seitenstruktur
    pz = seen.setdefault("pages_nonzero", {})
    since = (NOW - dt.timedelta(days=14)).date().isoformat()
    went = []
    for cid, n in (STATS.get("page_counts") or {}).items():
        if n:
            pz[cid] = TODAY
        elif n == 0 and (pz.get(cid) or "") >= since:
            went.append((watch.get(cid) or {}).get("name") or cid)
    STATS["pages_went_empty"] = went
    # Quelle nicht geladen und keine Stellen: Firma nicht in watch aufnehmen — sonst hält die Morgenaufgabe sie
    # für erledigt und prüft sie nicht (so fielen 05.–06.11. fünf Personio-Firmen still aus).
    failed = sorted(k for k, v in watch.items() if v.get("error") and not v.get("jobs_total"))
    for k in failed:
        del watch[k]
    STATS["watch_failed"] = failed
    # «blind»: Firma wird nur über die Jobbörse nach Name gesucht, dort gab es in 30 Tagen keine einzige Stelle von ihr
    blind = sorted(v["name"] for v in watch.values() if v.get("method") == "jobboerse" and not v.get("jobs_total"))
    STATS["blind_count"] = len(blind)
    write_dropped()

    # neu / schon gesehen (nur Information; die Tabelle gleicht selbst ab)
    for rec in prefer + other:
        rec["seen_before"] = rec["refnr"] in seen["refnr"]
        seen["refnr"][rec["refnr"]] = TODAY
    boards, STATS["stale"] = mark_stale(boards, prefer + other + [dict(j, company=j.get("company") or v.get("name"))
                                                                  for v in watch.values() for j in v.get("jobs") or []])
    # first_seen der Watch-Stellen und Boards (stamp_first_seen); seen.json und details.json auf 45 Tage begrenzen
    stamp = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff = (NOW - dt.timedelta(days=45)).date().isoformat()
    prev = load_json("today.json", {})
    wkey = lambda cid, j: f"{cid}|{j.get('id') or j.get('refnr')}"  # noqa: E731
    parts = {"watch": ([(wkey(c, j), j) for c, v in watch.items() for j in v.get("jobs") or []],
                       {wkey(c, j) for c, v in (prev.get("watch") or {}).items() for j in v.get("jobs") or []}),
             "boards": ([(board_key(b), b) for b in boards], {board_key(b) for b in prev.get("boards") or []})}
    pend = seen.get("pending") or {}
    again = {}
    for name, (items, pkeys) in parts.items():
        smap = seen.get(name)
        if smap is None:  # kein seen.json (data-Branch neu): was in der vorigen Datei stand, gilt als damals gesehen
            smap = {k: prev.get("generated") or stamp for k in pkeys}
        if name == "boards":
            # Stellen der vorigen Datei, deren Schlüssel sich geändert hat (Google Jobs bis 17.11.2026): first_seen
            # übernehmen, sonst gälten sie einmal als neu; sonst ohne Wirkung (die Schlüssel stehen schon in smap)
            for b in prev.get("boards") or []:
                if b.get("first_seen"):
                    smap.setdefault(board_key(b), b["first_seen"])
        pend[name], again[name] = stamp_first_seen(items, smap, pend.get(name), pkeys, prev.get("generated"), stamp)
        cur = {k for k, _ in items}
        seen[name] = {k: v for k, v in smap.items() if k in cur or v[:10] >= cutoff}
        pend[name] = {k: v for k, v in pend[name].items() if k in seen[name]}
    seen["pending"] = pend
    STATS["first_seen_again"] = again
    # Google Jobs: Ertrag je Abfrage (Rohtreffer, volle Seite, behalten, neu) — Grundlage, um die Abfragen zu gewichten,
    # wenn das Bonus-Guthaben aufgebraucht ist und nur noch halb so viele Suchen am Tag laufen
    sp = STATS.get("serpapi") or {}
    if sp.get("per_query") and not sp.get("from_cache"):
        for b in boards:
            if b.get("source") == "Google Jobs" and b.get("first_seen") == stamp:
                for tag in b.get("found_by") or []:
                    if tag in sp["per_query"]:
                        sp["per_query"][tag]["new"] += 1
        slog = load_json("serp-log.json", {})
        yld = slog.setdefault("yield", {})
        for tag, v in sp["per_query"].items():
            y = yld.setdefault(tag, {"runs": 0, "raw": 0, "full": 0, "kept": 0, "new": 0})
            y.update({"runs": y["runs"] + 1, "raw": y["raw"] + v["raw"], "full": y["full"] + int(v["more"]),
                      "kept": y["kept"] + v["kept"], "new": y["new"] + v["new"], "last": TODAY})
        save_json("serp-log.json", slog)
    seen["refnr"] = {k: v for k, v in seen["refnr"].items() if v >= cutoff}
    li_cutoff = (NOW - dt.timedelta(days=7)).date().isoformat()  # LinkedIn-Texte nur 7 Tage
    details_cache = {k: v for k, v in details_cache.items()
                     if (v.get("cached_at") or "") >= (li_cutoff if k.startswith("li:") else cutoff)}

    out = {
        "generated": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": TODAY,
        "days": CFG.get("days", 7),
        "schema": {
            "alerts": "Warnungen des Scanners (Quelle ausgefallen, unvollständig oder deutlich unter ihrem üblichen Wert) — in die Zusammenfassung übernehmen",
            "filters": "Titel-Regeln des Scanners (Regex, ohne Groß-/Kleinschreibung) für eigene Vorfilter, z. B. Stellen aus E-Mails",
            "blind": "beobachtete Firmen, von denen die Jobbörse seit 30 Tagen keine einzige Stelle zeigt — dort nicht sichtbar",
            "candidates": "Jobbörse, Titel passt zur Rolle (prefer): mit description",
            "other": "Jobbörse, Titel weder ausgeschlossen noch passend: nur Titel/Firma/Ort — kurz durchsehen",
            "watch": "beobachtete Firmen (Jobbörse nach Name, Personio, Recruitee, SmartRecruiters, Workday, JOIN, Ashby, Sitemap, d.vinci, Workable, Greenhouse, RSS, JSON, eigene Karriereseite = page): id → {name, method, jobs[]}; first_seen je Stelle = wann der Scanner sie zum ersten Mal sah (die Morgenaufgabe nimmt nur Stellen nach ihrem letzten Lauf; fehlte eine Stelle in der Datei, die die Morgenaufgabe las, zählt ihr Wiederauftauchen); jobs mit title_tier prefer haben meist description (Personio über search.json ohne); JOIN: id = Nummer aus der Stellen-URL, Liste vollständig (alle Seiten). Firmen, deren Quelle heute nicht lud, fehlen hier (stats.watch_failed) — selbst prüfen",
            "title_soft": "true an Stellen in candidates, watch und boards: der Titel enthält ein «weiches» Ausschlusswort (config title_soft: Montage, Kundenservice, Customer Success, Logistik, Gas, Wasser, Vertriebsinnendienst; oder Senior neben Junior; oder Senior nur als Möglichkeit — «(Senior) Projektmanager», «Projektleiter / Senior Projektleiter», config title_senior_optional: die Stelle ist auch ohne Senior ausgeschrieben, die Stufe nach dem Volltext beurteilen) und ein Rollenwort; behalten, weil es einen PV-/Speicherbezug gibt (eigene Liste einer beobachteten Firma, Wort im Titel/Firmennamen oder im Text; stats.title_soft). Kein Urteil über die Rolle — nach Fokus und Volltext entscheiden",
            "boards": "verify=true: Fundstelle nur auf einer Seite mit vielen veralteten Anzeigen (config stale_hosts) — zweite Fundstelle suchen, ohne sie nicht aufnehmen; eejobs/greenjobs (Atom), JobSpy (Indeed/LinkedIn), XING (öffentliche Suche) und Google Jobs (SerpApi); first_seen = wann der Scanner die Stelle zum ersten Mal sah (nur Stellen nach dem letzten eigenen Lauf nehmen); description = Volltext der Stelle (Google Jobs immer, LinkedIn/Indeed/XING wenn lesbar, PV/BESS darin geprüft) — dann danach bewerten, keine Kopie suchen; nur Titel, die nicht ausgeschlossen sind",
        },
        "candidates": sorted(prefer, key=lambda r: (r.get("km") is None, r.get("km") or 0, r.get("company") or "")),
        "other": [{k: r.get(k) for k in ("refnr", "title", "company", "location", "km", "published", "url", "homeoffice", "found_by")} for r in sorted(other, key=lambda r: (r.get("km") is None, r.get("km") or 0))],
        "watch": watch,
        "boards": boards,
        "blind": blind,
        "filters": {k: CFG.get(k) for k in ("title_exclude", "title_prefer", "title_adjacent", "title_core", "title_soft", "title_soft_proof", "title_senior_optional", "industry_strict_regex", "stale_hosts")},
        "stats": STATS,
        "errors": ERRORS,
        "minutes": round((time.time() - START) / 60, 1),
    }
    out["alerts"] = health(prefer, boards, out["minutes"])
    save_json("today.json", out)
    save_json("seen.json", seen)
    save_json("details.json", details_cache)
    save_json("run-log.json", {"date": TODAY, "generated": out["generated"], "minutes": out["minutes"], "stats": STATS, "errors": ERRORS, "alerts": out["alerts"],
                               "counts": {"candidates": len(prefer), "other": len(other), "watch": sum(len(v.get("jobs", [])) for v in watch.values()), "boards": len(boards)}})
    log("fertig:", json.dumps({"candidates": len(prefer), "other": len(other), "watch": len(watch), "boards": len(boards), "errors": len(ERRORS)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:  # noqa: BLE001
        err(f"Abbruch: {ex}")
        save_json("run-log.json", {"date": TODAY, "aborted": True, "errors": ERRORS, "stats": STATS})
        sys.exit(1)
