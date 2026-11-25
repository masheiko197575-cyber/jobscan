"""probe 10 (25.11.2026): Grundlagen für fünf Scanner-Änderungen. Nur öffentliche Seiten, die der Scanner ohnehin liest,
wenige Anfragen mit Pausen; kein SerpApi. Ergebnis: data/probe/probe10.json
A  LinkedIn-Stellentexte: warum sind sie teils nicht lesbar, hilft die Gast-Schnittstelle, hilft Warten?
B  LinkedIn-Suche regional (Stuttgart + Umkreis, breite Wörter) und nach Firmenname (Ersatz für Workday)
C  Indeed regional und nach Firmenname
D  XING regional (Ort + Radius) und nach Firmenname
E  eigene Karriereseiten der Workday-Firmen und zweier neuer Firmen: gibt es dort eine lesbare Liste?
"""
import json
import os
import re
import sys
import time

import requests

import scan  # nur Hilfsfunktionen; main() läuft nicht

OUT = {}
T0 = time.time()
ONLY = set(sys.argv[1:])


def want(sec):
    return not ONLY or sec in ONLY


def save():
    os.makedirs("data/probe", exist_ok=True)
    with open("data/probe/probe10.json", "w", encoding="utf-8") as f:
        json.dump(OUT, f, ensure_ascii=False, indent=1)


def say(*a):
    print(f"[{int(time.time() - T0):4d}s]", *a, flush=True)


MARK = re.compile(r'<div class="show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>', re.S)


def li_fetch(url):
    try:
        r = requests.get(url, headers=scan.LI_HEADERS, timeout=30)
    except Exception as ex:  # noqa: BLE001
        return {"exc": type(ex).__name__}
    m = MARK.search(r.text)
    rec = {"status": r.status_code, "final": re.sub(r"[?#].*$", "", r.url)[:80], "len": len(r.text), "markup": bool(m),
           "text_len": len(scan.strip_html(m.group(1))) if m else 0}
    if r.headers.get("Retry-After"):
        rec["retry_after"] = r.headers.get("Retry-After")
    if r.status_code == 200 and not m:
        t = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
        rec["title"] = scan.strip_html(t.group(1))[:90] if t else None
        rec["classes"] = sorted(set(re.findall(r'class="([^"]*(?:description|closed|expired|authwall)[^"]*)"', r.text)))[:6]
        rec["closed"] = bool(re.search(r"No longer accepting applications|Bewerbungen werden nicht mehr angenommen|closed-job", r.text))
    return rec


def li_ok(rec):
    return rec.get("status") == 200 and rec.get("markup") and rec.get("text_len", 0) > 0


# ---------------------------------------------------------------- A: LinkedIn-Texte
if want("A"):
    try:
        today = json.load(open("data/today.json", encoding="utf-8"))
        li = [b for b in today.get("boards", []) if b.get("source") == "LinkedIn"]

        def jid(b):
            m = re.search(r"/jobs/view/(?:[^/?#]*-)?(\d+)", b.get("url") or "")
            return m.group(1) if m else None

        notext = [jid(b) for b in li if not b.get("description") and jid(b)]
        withtext = [jid(b) for b in li if b.get("description") and jid(b)]
        ids = notext[:25] + withtext[:8]
        rows, failed = [], []
        for n, i in enumerate(ids):
            row = {"id": i, "had_text": i in withtext, "t": int(time.time() - T0)}
            row["view"] = li_fetch(f"https://www.linkedin.com/jobs/view/{i}")
            if not li_ok(row["view"]):
                time.sleep(1.5)
                row["guest"] = li_fetch(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{i}")
                if not li_ok(row["guest"]):
                    failed.append(i)
            rows.append(row)
            time.sleep(2.5)
        A = {"ids": len(ids), "no_text_ids": len(notext[:25]), "rows": rows,
             "view_ok": sum(1 for r in rows if li_ok(r["view"])),
             "guest_tried": sum(1 for r in rows if "guest" in r),
             "guest_ok": sum(1 for r in rows if "guest" in r and li_ok(r["guest"])),
             "failed_both": len(failed)}
        say("A erste Runde:", {k: v for k, v in A.items() if k != "rows"})
        OUT["A"] = A
        save()
        if failed:
            time.sleep(120)
            again = []
            for i in failed:
                row = {"id": i, "t": int(time.time() - T0), "view": li_fetch(f"https://www.linkedin.com/jobs/view/{i}")}
                if not li_ok(row["view"]):
                    time.sleep(1.5)
                    row["guest"] = li_fetch(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{i}")
                again.append(row)
                time.sleep(3)
            A["after_120s"] = {"tried": len(again), "ok": sum(1 for r in again if li_ok(r["view"]) or li_ok(r.get("guest") or {})),
                               "rows": again}
            say("A nach 120 s:", {k: v for k, v in A["after_120s"].items() if k != "rows"})
        save()
    except Exception as ex:  # noqa: BLE001
        OUT["A_error"] = f"{type(ex).__name__}: {str(ex)[:300]}"
        say("A Fehler", OUT["A_error"])
        save()


# ---------------------------------------------------------------- B, C: JobSpy
def spy(site, term, location, hours, want_n, distance=None, match=None):
    from jobspy import scrape_jobs  # type: ignore
    kw = dict(site_name=[site], search_term=term, location=location, country_indeed="germany", hours_old=hours,
              results_wanted=want_n, linkedin_fetch_description=False, description_format="markdown", verbose=0)
    if distance is not None:
        kw["distance"] = distance
    t = time.time()
    try:
        df = scrape_jobs(**kw)
    except Exception as ex:  # noqa: BLE001
        return {"term": term, "location": location, "error": f"{type(ex).__name__}: {str(ex)[:160]}"}
    rows = []
    for _, r in (df.iterrows() if df is not None and len(df) else []):
        loc = r.get("location")
        rows.append({"title": str(r.get("title") or "")[:90], "company": str(r.get("company") or "")[:50],
                     "location": "" if loc is None or isinstance(loc, float) else str(loc)[:50],
                     "url": str(r.get("job_url") or "")[:120], "has_desc": isinstance(r.get("description"), str) and len(r.get("description")) > 50})
    rec = {"term": term, "location": location, "distance": distance, "hours": hours, "n": len(rows), "s": round(time.time() - t, 1),
           "tiers": {}, "sample": rows[:14]}
    for x in rows:
        tier = scan.title_tier(x["title"])
        rec["tiers"][tier] = rec["tiers"].get(tier, 0) + 1
    locs = {}
    for x in rows:
        k = x["location"].split(",")[0]
        locs[k] = locs.get(k, 0) + 1
    rec["places"] = dict(sorted(locs.items(), key=lambda kv: -kv[1])[:12])
    if match:
        rx = re.compile(match, re.I)
        hit = [x for x in rows if rx.search(x["company"])]
        rec["company_match"] = len(hit)
        rec["sample"] = hit[:10] + [x for x in rows if x not in hit][:4]
    return rec


if want("B"):
    B = {"regional": [], "company": []}
    for term in ("Photovoltaik", "Batteriespeicher", "Erneuerbare Energien", "Netzanschluss"):
        B["regional"].append(spy("linkedin", term, "Stuttgart, Baden-Württemberg, Germany", 48, 40, distance=50))
        say("B regional", term, B["regional"][-1].get("n"), B["regional"][-1].get("tiers"), B["regional"][-1].get("error"))
        time.sleep(3)
    for term, match in (("BayWa r.e.", r"baywa"), ("GOLDBECK", r"goldbeck"), ("Canadian Solar", r"canadian solar|e-storage"),
                        ("GETEC", r"getec"), ("naturenergie", r"naturenergie|energiedienst"), ("Zeppelin Power Systems", r"zeppelin"),
                        ("Copenhagen Infrastructure Partners", r"copenhagen|\bcip\b"), ("Pramac", r"pramac|generac")):
        B["company"].append(spy("linkedin", term, "Germany", 168, 30, match=match))
        say("B Firma", term, B["company"][-1].get("n"), "passt:", B["company"][-1].get("company_match"), B["company"][-1].get("error"))
        time.sleep(3)
    OUT["B"] = B
    save()

if want("C"):
    C = {"regional": [], "company": []}
    for term in ("Photovoltaik", "Batteriespeicher", "Erneuerbare Energien"):
        C["regional"].append(spy("indeed", term, "Stuttgart", 72, 100, distance=50))
        say("C regional", term, C["regional"][-1].get("n"), C["regional"][-1].get("tiers"), C["regional"][-1].get("error"))
        time.sleep(2)
    for term, match in (("BayWa r.e.", r"baywa"), ("GOLDBECK SOLAR", r"goldbeck"), ("Canadian Solar", r"canadian solar|e-storage")):
        C["company"].append(spy("indeed", term, "Deutschland", 168, 40, match=match))
        say("C Firma", term, C["company"][-1].get("n"), "passt:", C["company"][-1].get("company_match"), C["company"][-1].get("error"))
        time.sleep(2)
    OUT["C"] = C
    save()

# ---------------------------------------------------------------- D: XING
if want("D"):
    D = []
    from urllib.parse import urlencode
    tests = [({"keywords": "Photovoltaik", "location": "Stuttgart", "radius": 70, "sincePeriod": "LAST_WEEK"}, None),
             ({"keywords": "Photovoltaik", "location": "Stuttgart", "sincePeriod": "LAST_WEEK"}, None),
             ({"keywords": "Photovoltaik", "location": "Stuttgart", "radius": 20, "sincePeriod": "LAST_WEEK"}, None),
             ({"keywords": "Batteriespeicher", "location": "Stuttgart", "radius": 70, "sincePeriod": "LAST_WEEK"}, None),
             ({"keywords": "BayWa r.e.", "location": "Deutschland", "sincePeriod": "LAST_WEEK"}, r"baywa"),
             ({"keywords": "GOLDBECK SOLAR", "location": "Deutschland", "sincePeriod": "LAST_WEEK"}, r"goldbeck")]
    for q, match in tests:
        page = scan.xing_get("https://www.xing.com/jobs/search?" + urlencode(q))
        rec = {"q": q, "ok": bool(page)}
        if page:
            cards = scan.xing_cards(page)
            m = re.search(r"([\d.]+)\s+(?:Jobs|Stellenangebote|Ergebnisse)", scan.strip_html(page[:200000]))
            rec.update({"cards": len(cards), "total_text": m.group(0) if m else None,
                        "places": {}, "sample": [{k: c[k] for k in ("title", "company", "location")} for c in cards[:10]]})
            for c in cards:
                k = (c["location"] or "").split(",")[0]
                rec["places"][k] = rec["places"].get(k, 0) + 1
            if match:
                rec["company_match"] = sum(1 for c in cards if re.search(match, c["company"], re.I))
        D.append(rec)
        say("D", q, rec.get("cards"), rec.get("total_text"), rec.get("places"), rec.get("company_match"))
        time.sleep(3)
    OUT["D"] = D
    save()

# ---------------------------------------------------------------- E: eigene Karriereseiten
if want("E"):
    PAGES = {
        "goldbeck": ["https://www.goldbeck.de/karriere/stellenangebote", "https://www.goldbeck.de/karriere/jobs"],
        "goldbeck-solar": ["https://goldbecksolar.com/karriere/", "https://goldbecksolar.com/de/karriere/"],
        "baywa-re": ["https://www.baywa-re.de/de/karriere/stellenangebote", "https://www.baywa-re.com/en/careers/job-offers",
                     "https://www.baywa-re.de/de/karriere"],
        "canadian-solar": ["https://www.csisolar.com/careers/", "https://www.csestorage.com/careers/"],
        "energiedienst": ["https://www.naturenergie.de/karriere", "https://karriere.naturenergie.de/",
                          "https://www.naturenergie.de/karriere/stellenangebote"],
        "getec": ["https://www.getec-energyservices.com/karriere/", "https://www.getec.de/karriere/",
                  "https://www.getec-energyservices.com/karriere/stellenangebote/"],
        "zeppelin": ["https://www.zeppelin.com/de-de/karriere/stellenangebote/", "https://www.zeppelin-powersystems.com/de/karriere"],
        "cip": ["https://www.cip.com/careers/"],
        "be-storaged": ["https://www.be-storaged.de/karriere", "https://www.be-storaged.com/karriere"],
        "giga-green": ["https://giga.green/karriere/", "https://giga.green/karriere", "https://www.giga.green/jobs",
                       "https://giga-green.jobs.personio.de/", "https://gigagreen.jobs.personio.de/", "https://join.com/companies/gigagreen",
                       "https://join.com/companies/giga-green"],
        "vbbw": ["https://www.vermoegenundbau-bw.de/karriere/stellenangebote", "https://www.vermoegenundbau-bw.de/karriere",
                 "https://www.vba-tuebingen.de/karriere"],
    }
    E = {}
    for cid, urls in PAGES.items():
        E[cid] = []
        for u in urls:
            rec = {"url": u}
            try:
                rec["robots_ok"] = scan.robots_ok(u)
                if rec["robots_ok"]:
                    r = requests.get(u, headers=scan.PAGE_UA, timeout=30)
                    txt = r.text or ""
                    rec.update({"status": r.status_code, "final": r.url[:120], "len": len(txt), "server": r.headers.get("Server"),
                                "cf": r.headers.get("Cf-Mitigated")})
                    links = []
                    for m in scan.A_TAG.finditer(txt):
                        h = scan.A_HREF.search(m.group(1))
                        href = next((g for g in (h.groups() if h else ()) if g), "")
                        text = scan.strip_html(m.group(2))[:80]
                        if re.search(r"\(m/w/d\)|\(w/m/d\)|\(m/f/d\)|\(all genders\)|m/w/d|w/m/d", text, re.I) or \
                           re.search(r"myworkdayjobs|/job/|/jobs/\d|stellenangebot[e]?/[^\"]{6,}|personio|join\.com/companies|softgarden|d-vinci|dvinci|recruitee|smartrecruiters|greenhouse|rexx|umantis|concludis|successfactors|/stelle/", href, re.I):
                            links.append({"t": text, "h": href[:140]})
                    rec["job_like_links"] = len(links)
                    rec["sample"] = links[:8]
                    rec["ats_hosts"] = sorted(set(re.findall(r"[a-z0-9.-]*(?:myworkdayjobs\.com|personio\.de|personio\.com|join\.com|softgarden\.io|dvinci-hr\.com|recruitee\.com|smartrecruiters\.com|greenhouse\.io|rexx-systems\.com|umantis\.com|successfactors\.[a-z]+|concludis\.de|onlyfy\.jobs|teamtailor\.com|workable\.com|ashbyhq\.com|interamt\.de|stellen\.[a-z-]+\.de)", txt, re.I)))[:8]
                    rec["jsonld_jobposting"] = txt.count('"JobPosting"')
            except Exception as ex:  # noqa: BLE001
                rec["error"] = f"{type(ex).__name__}: {str(ex)[:160]}"
            E[cid].append(rec)
            say("E", cid, {k: rec.get(k) for k in ("url", "status", "robots_ok", "job_like_links", "ats_hosts", "error")})
            time.sleep(2)
    OUT["E"] = E
    save()

OUT["seconds"] = int(time.time() - T0)
save()
say("fertig")
