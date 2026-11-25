"""probe 11 (25.11.2026): die neuen Teile des Scanners gegen die echten Seiten — ohne den ganzen Scan und ohne SerpApi.
Neue Firmen (Personio GIGA.GREEN und GETEC, Karriereseite Vermögen und Bau BW), regionale Durchgänge (Indeed, LinkedIn,
XING), Ersatzsuche nach Firmenname für die Workday-Firmen, spätere Leserunden für LinkedIn-Texte.
Ergebnis: data/probe/probe11.json"""
import json
import os
import time

import scan

OUT = {}
T0 = time.time()


def save():
    os.makedirs("data/probe", exist_ok=True)
    with open("data/probe/probe11.json", "w", encoding="utf-8") as f:
        json.dump(OUT, f, ensure_ascii=False, indent=1)


def say(*a):
    print(f"[{int(time.time() - T0):4d}s]", *a, flush=True)


def brief(j):
    return {"title": (j.get("title") or "")[:100], "company": (j.get("company") or "")[:40], "loc": (j.get("location") or "")[:40],
            "tier": j.get("title_tier"), "text": len(j.get("description") or j.get("_text") or ""), "by": (j.get("found_by") or [""])[0][:60],
            "url": (j.get("url") or "")[:110], "published": j.get("published")}


try:
    dc = json.load(open("data/details.json", encoding="utf-8"))
except Exception:  # noqa: BLE001
    dc = {}
OUT["details_cache_in"] = len(dc)

# ---------------------------------------------------------------- 1: neue Firmen
try:
    for e in scan.CFG["personio"]:
        if e["id"] in ("giga-green", "getec"):
            jobs = scan.scan_personio(e)
            pv = [j for j in jobs or [] if scan.PV_ONLY.search(j.get("title") or "") or len(scan.PV_ONLY.findall(j.get("description") or "")) >= 2]
            OUT[f"personio_{e['id']}"] = {"n": None if jobs is None else len(jobs), "pv_only_n": len(pv),
                                          "offices": sorted({j.get("location") or "" for j in jobs or []})[:20],
                                          "tiers": {t: sum(1 for j in jobs or [] if j["title_tier"] == t) for t in ("prefer", "other", "drop")},
                                          "sample": [brief(j) for j in (jobs or []) if j["title_tier"] != "drop"][:25],
                                          "pv_sample": [brief(j) for j in pv][:15]}
            say("Personio", e["id"], OUT[f"personio_{e['id']}"]["n"], OUT[f"personio_{e['id']}"]["tiers"], "pv:", len(pv))
    for e in scan.CFG["page"]:
        if e["id"] == "vermoegen-und-bau-bw":
            cache = {}
            jobs = scan.scan_page(e, cache)
            pv = [j for j in jobs or [] if scan.PV_ONLY.search(j.get("title") or "") or len(scan.PV_ONLY.findall(j.get("_text") or j.get("description") or "")) >= 2]
            OUT["page_vbbw"] = {"n": None if jobs is None else len(jobs), "pv_only_n": len(pv), "pages_read": len(cache),
                                "tiers": {t: sum(1 for j in jobs or [] if j["title_tier"] == t) for t in ("prefer", "other", "drop")},
                                "with_text": sum(1 for j in jobs or [] if j.get("_text")),
                                "text_from": sorted({str(v.get("text_from")) for v in cache.values()}),
                                "sample": [brief(j) for j in (jobs or [])][:30], "pv_sample": [brief(j) for j in pv][:15],
                                "errors": list(scan.ERRORS)[-5:]}
            say("Seite VBBW", OUT["page_vbbw"]["n"], OUT["page_vbbw"]["tiers"], "pv:", len(pv), "Seiten gelesen:", len(cache))
except Exception as ex:  # noqa: BLE001
    OUT["firms_error"] = f"{type(ex).__name__}: {str(ex)[:300]}"
    say("Firmen Fehler", OUT["firms_error"])
save()

# ---------------------------------------------------------------- 2: XING regional (bundesweit nur ein Begriff)
try:
    scan.CFG["xing"]["terms"] = ["Projektmanager Photovoltaik"]
    scan.CFG["xing"]["broad_terms"] = []
    xb = scan.scan_xing(dc)
    reg = [b for b in xb if any(f.startswith("xing Stuttgart") for f in b["found_by"])]
    OUT["xing"] = {"stats": scan.STATS.get("xing"), "kept": len(xb), "regional_kept": len(reg), "regional": [brief(b) for b in reg][:40]}
    say("XING", scan.STATS.get("xing"), "regional behalten:", len(reg))
except Exception as ex:  # noqa: BLE001
    OUT["xing_error"] = f"{type(ex).__name__}: {str(ex)[:300]}"
    say("XING Fehler", OUT["xing_error"])
save()

# ---------------------------------------------------------------- 3: Indeed + LinkedIn regional, Ersatzsuche, Leserunden
try:
    scan.CFG["indeed"]["terms"] = ["Projektmanager Photovoltaik"]
    scan.CFG["indeed"]["broad_terms"] = []
    scan.CFG["jobspy"]["terms"] = ["Projektmanager Photovoltaik"]
    ids = list(dict.fromkeys(e["id"] for e in scan.CFG["workday"] if e["id"] != "be-storaged"))
    scan.STATS["workday"] = {"failed_ids": ids, "failed": ids, "blocked": 1}
    scan.FEEDS_DONE.set()
    t = time.time()
    boards = scan.boards_jobspy(dc)
    OUT["jobspy_seconds"] = int(time.time() - t)
    ind = [b for b in boards if b["source"] == "Indeed"]
    li = [b for b in boards if b["source"] == "LinkedIn"]
    OUT["indeed"] = {"stats": scan.STATS.get("indeed"), "kept": len(ind),
                     "regional": [brief(b) for b in ind if any(f.startswith("indeed Stuttgart") for f in b["found_by"])][:40]}
    OUT["linkedin"] = {"stats": scan.STATS.get("jobspy"), "kept": len(li), "without_text": sum(1 for b in li if not b.get("description")),
                       "regional": [brief(b) for b in li if any(f.startswith("linkedin Stuttgart") for f in b["found_by"])][:40]}
    fb = {}
    for cid in scan.FALLBACK:
        raw_n = len(scan.FALLBACK[cid].get("jobs") or [])
        jobs = scan.fallback_jobs(cid)
        fb[cid] = {"ok": scan.FALLBACK[cid].get("ok"), "via": scan.FALLBACK[cid].get("via"), "found": raw_n, "after_pv_only": len(jobs),
                   "not_dropped": sum(1 for j in jobs if j["title_tier"] != "drop"),
                   "jobs": [brief(j) for j in jobs if j["title_tier"] != "drop"][:15]}
    OUT["fallback"] = {"stats": scan.STATS.get("fallback"), "companies": fb}
    say("Indeed", scan.STATS.get("indeed"))
    say("LinkedIn", scan.STATS.get("jobspy"), "ohne Text:", OUT["linkedin"]["without_text"], "von", len(li))
    say("Ersatzsuche", {k: (v["found"], v["not_dropped"]) for k, v in fb.items()})
except Exception as ex:  # noqa: BLE001
    import traceback
    OUT["jobspy_error"] = f"{type(ex).__name__}: {str(ex)[:300]} | {traceback.format_exc()[-600:]}"
    say("JobSpy Fehler", OUT["jobspy_error"])
OUT["errors"] = list(scan.ERRORS)[:40]
OUT["seconds"] = int(time.time() - T0)
save()
say("fertig", len(scan.ERRORS), "Fehler")
