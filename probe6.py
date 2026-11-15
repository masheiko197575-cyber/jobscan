#!/usr/bin/env python3
"""Probe 6 (15.11.2026): neue Leser des Scanners mit den neuen Firmen aus config.json einzeln ausführen (ohne seen,
ohne today.json) und dazu drei offene Seiten ansehen. Ergebnis: data/probe/p6.json"""
import json
import os
import re
import time

import requests

import scan

OUT = os.path.join(scan.DATA, "probe")
os.makedirs(OUT, exist_ok=True)
NEW = set(json.load(open("probe5_ids.json", encoding="utf-8")))
cache = {}
res = {"readers": {}, "explore": {}}
kinds = {"join": lambda e: scan.scan_join(e, cache), "personio": scan.scan_personio, "workday": scan.scan_workday,
         "sitemap": lambda e: scan.scan_sitemap(e, cache), "dvinci": scan.scan_dvinci, "workable": scan.scan_workable,
         "greenhouse": scan.scan_greenhouse, "rss": scan.scan_rss, "jsonlist": scan.scan_jsonlist, "page": lambda e: scan.scan_page(e, cache)}
t_all = time.time()
for kind, fn in kinds.items():
    for e in scan.CFG.get(kind, []):
        if e["id"] not in NEW:
            continue
        t0, n_err = time.time(), len(scan.ERRORS)
        try:
            jobs = fn(e)
        except Exception as ex:  # noqa: BLE001
            jobs = None
            scan.ERRORS.append(f"CRASH {type(ex).__name__}: {ex}")
        pv = None
        if jobs and e.get("pv_only"):
            pv = [j["title"][:70] for j in jobs if scan.PV_ONLY.search(j.get("title") or "")
                  or len(scan.PV_ONLY.findall(j.get("_text") or j.get("description") or "")) >= 2]
        res["readers"][f"{kind}:{e['id']}"] = {
            "n": None if jobs is None else len(jobs), "pv_kept": pv, "sec": round(time.time() - t0, 1), "errors": scan.ERRORS[n_err:][:6],
            "tiers": {t: sum(1 for j in jobs or [] if j.get("title_tier") == t) for t in ("prefer", "other", "drop")},
            "jobs": [[j.get("id"), (j.get("title") or "")[:90], j.get("title_tier"), (j.get("location") or "")[:40], len(j.get("description") or ""),
                      len(j.get("_text") or ""), j.get("published"), (j.get("url") or "")[-100:]] for j in (jobs or [])[:70]]}
        print(kind, e["id"], res["readers"][f"{kind}:{e['id']}"]["n"], res["readers"][f"{kind}:{e['id']}"]["sec"], "s", flush=True)
res["sec_readers"] = round(time.time() - t_all)
res["cache_pg"] = sum(1 for k in cache if k.startswith("pg:"))
res["text_from"] = {}
for k, v in cache.items():
    if k.startswith("pg:"):
        host = re.sub(r"^pg:https?://", "", k).split("/")[0]
        tf = v.get("text_from") or "ld"
        res["text_from"].setdefault(host, {}).setdefault(tf, 0)
        res["text_from"][host][tf] += 1

UA = scan.PAGE_UA


def snip(text, pat, n=5, w=200):
    return [text[max(0, m.start() - w):m.end() + w] for m in list(re.finditer(pat, text or "", re.I))[:n]]


def safe(name, fn):
    try:
        res["explore"][name] = fn()
    except Exception as ex:  # noqa: BLE001
        res["explore"][name] = {"crash": f"{type(ex).__name__}: {ex}"[:300]}


def transnetbw2():
    out = {}
    t = scan.page_get("https://jobs.transnetbw.de/Jobs/All", quiet=True) or ""
    links = scan.page_links("https://jobs.transnetbw.de/Jobs/All", t)
    out["umantis"] = {"len": len(t), "vac": [[lab[:80], u[-80:]] for u, lab, alt in links if "/Vacancies/" in u][:30]}
    js = scan.page_get("https://webservices.transnetbw.de/jobs/app/jobportal.js?v20260610", quiet=True) or ""
    out["js_snips"] = [js[max(0, m.start() - 300):m.end() + 300] for m in list(re.finditer(r"jobs/json/", js))[:3]]
    for u in ("https://webservices.transnetbw.de/jobs/json/", "https://webservices.transnetbw.de/jobs/json/jobs.json",
              "https://webservices.transnetbw.de/jobs/json/de.json"):
        try:
            r = requests.get(u, headers=UA, timeout=20)
            out[u] = [r.status_code, r.headers.get("content-type"), r.text[:400]]
        except Exception as ex:  # noqa: BLE001
            out[u] = str(ex)[:150]
    return out


for name, fn in (("transnetbw2", transnetbw2),):
    safe(name, fn)
res["errors_all"] = scan.ERRORS[:80]
json.dump(res, open(os.path.join(OUT, "p6.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("fertig", round(time.time() - t_all), "s")
