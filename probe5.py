#!/usr/bin/env python3
"""Probe 5 (15.11.2026): neue Leser des Scanners mit den neuen Firmen aus config.json einzeln ausführen (ohne seen,
ohne today.json) und dazu drei offene Seiten ansehen. Ergebnis: data/probe/p5.json"""
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


def prokon():
    t = scan.page_get("https://stellenanzeigen.prokon.energy/de") or ""
    links = scan.page_links("https://stellenanzeigen.prokon.energy/de", t)
    return {"len": len(t), "title": (re.search(r"<title[^>]*>(.*?)</title>", t, re.S | re.I) or [None, ""])[1][:80],
            "links": [[lab[:80], u[-110:]] for u, lab, alt in links if len(lab) > 8][:40],
            "ats": sorted(set(re.findall(r"[a-z0-9.-]+\.(?:rexx-systems|softgarden|personio|dvinci-hr|onlyfy|recruitee|join|umantis|concludis|jobbase)\.[a-z]+", t, re.I)))[:6],
            "json_urls": sorted(set(re.findall(r"[\"'](/[^\"'\s]*(?:api|json)[^\"'\s]*)[\"']", t)))[:10]}


def transnetbw():
    t = scan.page_get("https://www.transnetbw.de/de/karriere/stellenangebote") or ""
    js = re.findall(r"<script[^>]+src=[\"']([^\"']*webservices\.transnetbw\.de[^\"']*)", t, re.I)
    out = {"snips": [s[:500] for s in snip(t, r"webservices\.transnetbw\.de", 4, 250)], "js": js[:4]}
    for u in js[:2]:
        jt = scan.page_get(u if u.startswith("http") else "https:" + u, quiet=True) or ""
        out.setdefault("js_urls", []).extend(sorted(set(re.findall(r"https?://[^\"'\s)]+", jt)))[:25])
        out.setdefault("js_paths", []).extend(sorted(set(re.findall(r"[\"'](/[A-Za-z0-9_/.-]*(?:job|stelle|career|karriere|vacanc)[A-Za-z0-9_/.?=&-]*)[\"']", jt, re.I)))[:20])
    return out


def bwess():
    t = scan.page_get("https://bw-ess.com/career") or ""
    tok = sorted(set(re.findall(r"greenhouse\.io/[^\"'\s]*?for=([A-Za-z0-9_-]+)", t)))
    tok += sorted(set(re.findall(r"boards(?:\.eu)?\.greenhouse\.io/([A-Za-z0-9_-]+)", t)))
    out = {"tokens": tok[:6], "snips": [s[:400] for s in snip(t, r"greenhouse", 3, 150)]}
    for tk in tok[:3]:
        for api in ("boards-api.eu.greenhouse.io", "boards-api.greenhouse.io"):
            jobs = scan.scan_greenhouse({"id": "bw-energy-storage", "name": "BW ESS", "board": tk, "api": api, "location_regex": "."})
            out[f"{api}/{tk}"] = None if jobs is None else [[j["title"][:70], j["location"]] for j in jobs[:30]]
    return out


def albwerk():
    d = scan.page_get("https://albwerk-karriere.de/jobPublication/list.json", as_json=True)
    if not isinstance(d, list):
        return {"list": None}
    return {"n": len(d), "items": [[p.get("position"), ((p.get("jobOpening") or {}).get("name") or "")[:90],
                                    sorted((p.get("jobOpening") or {}).keys())[:20]] for p in d[:40]]}


def encavis():
    out = {}
    for h in ("encavis.dvinci-hr.com", "encavis-karriere.dvinci-hr.com", "karriere.encavis.com"):
        try:
            r = requests.get(f"https://{h}/jobPublication/list.json", headers=UA, timeout=20)
            out[h] = [r.status_code, r.text[:120]]
        except Exception as ex:  # noqa: BLE001
            out[h] = str(ex)[:120]
    return out


for name, fn in (("prokon", prokon), ("transnetbw", transnetbw), ("bwess", bwess), ("albwerk", albwerk), ("encavis", encavis)):
    safe(name, fn)
res["errors_all"] = scan.ERRORS[:80]
json.dump(res, open(os.path.join(OUT, "p5.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("fertig", round(time.time() - t_all), "s")
