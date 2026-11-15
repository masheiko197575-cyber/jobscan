#!/usr/bin/env python3
"""Probe 3 (15.11.2026): Karriereseiten der Firmen ohne eigene Liste im Scanner.
Je Firma: robots.txt, Antwort der Seite, Stellensystem (ATS), Gruppen von Stellen-Links. Dazu die schon bekannten
Leser (JOIN, Personio, Workday) für Firmen, die bisher nur in der Tabelle standen. Ergebnis: data/probe/pages.json"""
import html
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests

import scan

OUT = os.path.join(scan.DATA, "probe")
os.makedirs(OUT, exist_ok=True)
UA = {**scan.UA, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}
AGENT = "jobscan"
CFG = json.load(open("targets.json", encoding="utf-8"))
MAX_PAGES = 11

_rb, _rb_lock, _host_last, _host_lock = {}, threading.Lock(), {}, threading.Lock()


def wait_host(url):
    host = urlparse(url).netloc
    with _host_lock:
        t = _host_last.get(host, 0)
        now = time.time()
        delay = max(0.0, t + 1.2 - now)
        _host_last[host] = now + delay
    if delay:
        time.sleep(delay)


def robots_ok(url):
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    with _rb_lock:
        known = base in _rb
    if not known:
        val = None
        try:
            wait_host(base)
            r = requests.get(base + "/robots.txt", headers=UA, timeout=15)
            if r.status_code in (401, 403):
                val = "forbidden"
            elif r.status_code == 200 and "<html" not in r.text[:300].lower():
                rp = robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
                val = rp
        except Exception:  # noqa: BLE001
            val = None
        with _rb_lock:
            _rb[base] = val
    rp = _rb[base]
    if rp is None:
        return True
    if rp == "forbidden":
        return False
    try:
        return rp.can_fetch(AGENT, url)
    except Exception:  # noqa: BLE001
        return True


def get(url):
    """GET mit eigener Kennung; robots.txt wird bei jedem Sprung geprüft, Weiterleitungen von Hand (höchstens 5)."""
    cur = url
    for _ in range(6):
        if not robots_ok(cur):
            return {"url": url, "final": cur, "robots": False}
        try:
            wait_host(cur)
            r = requests.get(cur, headers=UA, timeout=20, allow_redirects=False)
        except Exception as e:  # noqa: BLE001
            return {"url": url, "final": cur, "err": f"{type(e).__name__}: {str(e)[:90]}"}
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
            cur = urljoin(cur, r.headers["location"])
            continue
        text = ""
        if r.status_code == 200 and len(r.content) < 4_000_000:
            r.encoding = r.encoding if (r.encoding and r.encoding.lower() != "iso-8859-1") else (r.apparent_encoding or "utf-8")
            text = r.text
        return {"url": url, "final": cur, "status": r.status_code, "ctype": (r.headers.get("content-type") or "")[:40], "text": text}
    return {"url": url, "final": cur, "err": "zu viele Weiterleitungen"}


A_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
HREF_RE = re.compile(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
TITLE_RE = re.compile(r"""(?:title|aria-label)\s*=\s*(?:"([^"]+)"|'([^']+)')""", re.I)


def anchors(base, text):
    out = []
    for m in A_RE.finditer(text):
        h = HREF_RE.search(m.group(1))
        if not h:
            continue
        href = html.unescape(next(g for g in h.groups() if g is not None)).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        txt = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", m.group(2)))).strip()
        t2 = TITLE_RE.search(m.group(1))
        alt = html.unescape(next(g for g in t2.groups() if g)) if t2 else ""
        out.append((urljoin(base, href).split("#")[0], txt, alt))
    return out


ATS = {
    "personio": r"[a-z0-9-]+\.jobs\.personio\.(?:de|com)",
    "join": r"join\.com/companies/[a-z0-9-]+",
    "recruitee": r"[a-z0-9-]+\.recruitee\.com",
    "smartrecruiters": r"(?:careers|jobs|api)\.smartrecruiters\.com/[A-Za-z0-9-]+",
    "workday": r"[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com(?:/[A-Za-z0-9_-]+){0,2}",
    "ashby": r"jobs\.ashbyhq\.com/[A-Za-z0-9-]+",
    "dvinci": r"[a-z0-9-]+\.dvinci-hr\.com",
    "softgarden": r"[a-z0-9-]+\.softgarden\.io",
    "rexx": r"[a-z0-9.-]+\.rexx-(?:systems|recruitment)\.com|rexx[ _-]?(?:systems|jobportal|recruit)",
    "umantis": r"recruitingapp-\d+\.(?:de\.)?umantis\.com",
    "successfactors": r"career\d*\.successfactors\.(?:eu|com)/[^\"'\s<>)]{0,90}|jobs2web|rmkcdn\.successfactors",
    "teamtailor": r"[a-z0-9-]+\.teamtailor\.com|teamtailor-cdn|career\.teamtailor",
    "workable": r"apply\.workable\.com/[a-z0-9-]+",
    "greenhouse": r"(?:boards|boards-api|job-boards)\.greenhouse\.io/[a-z0-9]+",
    "lever": r"jobs\.lever\.co/[a-z0-9-]+",
    "onlyfy": r"[a-z0-9-]+\.onlyfy\.jobs|prescreenapp\.io|prescreen\.io|onlyfy",
    "jobvite": r"jobs\.jobvite\.com/[a-z0-9-]+",
    "bamboohr": r"[a-z0-9-]+\.bamboohr\.com",
    "breezy": r"[a-z0-9-]+\.breezy\.hr",
    "concludis": r"[a-z0-9-]+\.concludis\.de|concludis",
    "hr4you": r"hr4you",
    "coveto": r"[a-z0-9-]+\.coveto\.de",
    "heyrecruit": r"heyrecruit",
    "talention": r"talention",
    "jobbase": r"[a-z0-9-]+\.jobbase\.io",
    "factorial": r"[a-z0-9-]+\.factorialhr\.(?:de|com)",
    "phenom": r"phenompeople|cdn\.phenom",
    "icims": r"[a-z0-9-]+\.icims\.com",
    "taleo": r"[a-z0-9-]+\.taleo\.net",
    "connectoor": r"connectoor",
    "kenjo": r"kenjo\.io",
    "beesite": r"beesite",
    "softgarden_feed": r"jobs\.feed\.json|/job-feed",
    "talentsconnect": r"talentsconnect",
    "dvinci_widget": r"dvinci-job-[a-z0-9.-]+",
    "jobs_api": r"[\"'](?:https?:)?//[^\"'\s]{4,80}/(?:api|wp-json)/[^\"'\s]{0,60}(?:job|stellen|vacanc)[^\"'\s]{0,40}[\"']",
}
ATS_RE = {k: re.compile(v, re.I) for k, v in ATS.items()}

JOBTXT = re.compile(r"\(\s*[mwdfxi](?:\s*[/|*,]\s*[mwdfxi]){1,3}\s*\)|\ball genders\b|\(gn\)|\bm/w/d\b|\bw/m/d\b|\bd/m/w\b|\(alle\)|\bm/f/d\b|\bf/m/d\b", re.I)
JOBHREF = re.compile(r"/(?:job|jobs|stellen\w*|stellenangebot\w*|stellenanzeige\w*|vacanc\w*|position\w*|offene-stellen|karriere|career\w*|jobpublication|job_posting|job-positions|stellenausschreibung\w*|jobdetail\w*|detail\w*)/[^?#]{4,}"
                     r"|[?&](?:id|jobid|job_id|yid|jobId|tx_[a-z_]+%5B[a-z]+%5D)=[\w-]{2,}|/j/\w{4,}|stellen[^?#]*\.pdf$|/[^/?#]*(?:stellenausschreibung|stellenanzeige)[^/?#]*\.pdf$", re.I)
CAREER = re.compile(r"karriere|career|jobs?\b|jobs|stellen|vacanc|offene|arbeiten-bei|join-us|work-with|bewerb|recruit", re.I)
CAREER_HI = re.compile(r"stellen|jobs|vacanc|offene|job-overview|stellenmarkt|positions|search", re.I)
NAV = re.compile(r"^(?:mehr|weiter|details?|zur stelle|jetzt bewerben|bewerben|apply( now)?|read more|mehr erfahren|zurück|home|kontakt|impressum|datenschutz|karriere|jobs|stellenangebote|alle stellen|de|en|learn more|more)?$", re.I)
SKIP_EXT = re.compile(r"\.(?:jpg|jpeg|png|gif|svg|webp|css|js|zip|mp4|ico|woff2?)(?:\?|$)", re.I)


def pattern(u):
    p = urlparse(u)
    norm = []
    for s in [x for x in p.path.split("/") if x]:
        norm.append("*" if (re.search(r"\d", s) or len(s) > 24 or s.count("-") >= 2 or "%" in s or s.lower().endswith(".pdf")) else s.lower())
    q = "&".join(sorted({k.split("=")[0] for k in p.query.split("&") if k}))
    return f"{p.netloc}/" + "/".join(norm) + (f"?{q}" if q else "")


def clusters(page_url, anc):
    groups = {}
    seen = set()
    for url, txt, alt in anc:
        if url in seen or url.rstrip("/") == page_url.rstrip("/") or SKIP_EXT.search(url):
            continue
        label = txt if not NAV.match(txt) else (alt or txt)
        jt = bool(JOBTXT.search(f"{txt} {alt}"))
        if not (jt or JOBHREF.search(url)):
            continue
        seen.add(url)
        g = groups.setdefault(pattern(url), {"n": 0, "jt": 0, "samples": []})
        g["n"] += 1
        g["jt"] += jt
        if len(g["samples"]) < 6:
            g["samples"].append([label[:80], url[-110:]])
    top = sorted(groups.items(), key=lambda kv: -(kv[1]["jt"] * 3 + kv[1]["n"]))[:3]
    return [{"pat": k, **v} for k, v in top]


def json_summary(text):
    try:
        d = json.loads(text)
    except Exception:  # noqa: BLE001
        return None
    best = None

    def walk(x, path, depth):
        nonlocal best
        if depth > 4:
            return
        if isinstance(x, list) and x and isinstance(x[0], dict):
            keys = set().union(*(set(i.keys()) for i in x[:5] if isinstance(i, dict)))
            tk = next((k for k in ("title", "name", "jobTitle", "position", "text", "jobName", "externalPostingName") if k in keys), None)
            if tk and (best is None or len(x) > best["n"]):
                best = {"path": path, "n": len(x), "title_key": tk, "keys": sorted(keys)[:25],
                        "samples": [str(i.get(tk))[:70] for i in x[:5] if isinstance(i, dict)]}
            for i in x[:2]:
                walk(i, path + "[]", depth + 1)
        elif isinstance(x, dict):
            for k, v in list(x.items())[:40]:
                walk(v, f"{path}.{k}", depth + 1)
    walk(d, "", 0)
    return best or {"top": (sorted(d.keys())[:15] if isinstance(d, dict) else f"list[{len(d)}]")}


def ats_followups(marks):
    out = []
    for h in marks.get("dvinci", [])[:2]:
        if h.endswith(".dvinci-hr.com"):
            out += [f"https://{h}/de/jobs", f"https://{h}/jobPublication/list.json"]
    for h in marks.get("softgarden", [])[:2]:
        if not h.startswith(("www.", "static", "cdn", "tracker", "certificate", "mediaassets")):
            out += [f"https://{h}/de/vacancies", f"https://{h}/vacancies"]
    for h in marks.get("umantis", [])[:1]:
        out.append(f"https://{h}/Jobs/All")
    for h in marks.get("personio", [])[:1]:
        out.append(f"https://{h}/search.json")
    for h in marks.get("teamtailor", [])[:1]:
        if h.endswith(".teamtailor.com"):
            out.append(f"https://{h}/jobs")
    for h in marks.get("recruitee", [])[:1]:
        out.append(f"https://{h}/api/offers/")
    for h in marks.get("workable", [])[:1]:
        out.append("https://apply.workable.com/api/v1/widget/accounts/" + h.rsplit("/", 1)[-1])
    for h in marks.get("greenhouse", [])[:1]:
        out.append("https://boards-api.greenhouse.io/v1/boards/" + h.rsplit("/", 1)[-1] + "/jobs")
    for h in marks.get("lever", [])[:1]:
        out.append("https://api.lever.co/v0/postings/" + h.rsplit("/", 1)[-1] + "?mode=json")
    for h in marks.get("bamboohr", [])[:1]:
        if not h.startswith(("www.", "resources.")):
            out.append(f"https://{h}/careers/list")
    for h in marks.get("onlyfy", [])[:1]:
        if h.endswith(".onlyfy.jobs"):
            out.append(f"https://{h}/")
    for h in marks.get("rexx", [])[:1]:
        if "." in h and " " not in h:
            out.append(f"https://{h}/stellenangebote.html")
    for h in marks.get("successfactors", [])[:1]:
        if h.lower().startswith("career"):
            out.append("https://" + h)
    return out


def analyse(res):
    """Seite → Kurzbefund (ohne den Text selbst)."""
    text = res.pop("text", "") or ""
    info = {k: v for k, v in res.items()}
    if not text:
        return info, [], {}
    info["len"] = len(text)
    marks = {}
    for k, rx in ATS_RE.items():
        found = sorted({m.group(0).lower().strip("\"'") for m in rx.finditer(text)})[:4]
        if found:
            marks[k] = found
    info["ats"] = marks
    head = text[:400].lstrip()
    if head.startswith(("{", "[")):
        info["json"] = json_summary(text)
        return info, [], marks
    if "<loc>" in text and ("<urlset" in text or "<sitemapindex" in text):
        locs = [html.unescape(u) for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text)]
        groups = {}
        for u in locs:
            g = groups.setdefault(pattern(u), {"n": 0, "samples": []})
            g["n"] += 1
            if len(g["samples"]) < 4:
                g["samples"].append(u[-120:])
        info["sitemap"] = {"locs": len(locs), "index": "<sitemapindex" in text,
                           "de": sum(1 for u in locs if re.search(r"german|deutsch|[-_/]de[-_/]", u, re.I)),
                           "top": [{"pat": k, **v} for k, v in sorted(groups.items(), key=lambda kv: -kv[1]["n"])[:4]]}
        return info, [], marks
    anc = anchors(res.get("final") or res["url"], text)
    info["links"] = len(anc)
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    info["title"] = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()[:70] if m else ""
    info["jsonld"] = text.count('"JobPosting"')
    info["next"] = bool(re.search(r"rel=[\"']next[\"']|[?&](?:page|p|startrow|offset|seite)=\d", text, re.I))
    info["clusters"] = clusters(res.get("final") or res["url"], anc)
    info["scripts_only"] = bool(len(anc) < 15 and re.search(r"<div[^>]+id=[\"'](?:app|root|__next|__nuxt)[\"']", text))
    return info, anc, marks


def probe_company(t):
    pages, visited, paths, queue = [], set(), set(), [(u, 9) for u in t["start"]]
    site = None
    while queue and len(pages) < MAX_PAGES:
        queue.sort(key=lambda x: -x[1])
        url, score = queue.pop(0)
        key = url.rstrip("/")
        if key in visited:
            continue
        visited.add(key)
        paths.add(url.split("?")[0].rstrip("/"))
        res = get(url)
        if res.get("final"):
            visited.add(res["final"].rstrip("/"))
        info, anc, marks = analyse(res)
        info["score"] = score
        pages.append(info)
        if not anc and not marks:
            continue
        final = info.get("final") or url
        if site is None:
            site = ".".join(urlparse(final).netloc.split(".")[-2:])
        for u in ats_followups(marks):
            if u.rstrip("/") not in visited:
                queue.append((u, 8))
        if score >= 5:  # von Start- und Karriereseiten weitergehen, nicht von Stellenseiten
            for u, txt, alt in anc:
                host = urlparse(u).netloc
                if SKIP_EXT.search(u) or u.rstrip("/") in visited or not u.startswith("http"):
                    continue
                same = host.endswith(site or "\0")
                label = f"{txt} {alt}"
                if not (CAREER.search(urlparse(u).path) or CAREER.search(label[:60])):
                    continue
                if not same and not any(rx.search(u) for rx in ATS_RE.values()) and not CAREER.search(host):
                    continue
                if JOBTXT.search(label) or len(urlparse(u).path) > 90:
                    continue  # einzelne Stelle
                s = 7 if (CAREER_HI.search(urlparse(u).path) or CAREER_HI.search(label[:60])) else 6
                if score <= 7:
                    s -= 2
                bare = u.split("?")[0].rstrip("/")
                if bare in paths or any(q[0].split("?")[0].rstrip("/") == bare for q in queue):
                    continue  # dieselbe Seite mit anderen Parametern (Blättern, Filter)
                queue.append((u, s))
    best, best_score = None, 0
    for i, p in enumerate(pages):
        c = (p.get("clusters") or [{}])[0] if p.get("clusters") else {}
        sc = c.get("jt", 0) * 3 + c.get("n", 0)
        if p.get("json") and p["json"].get("n"):
            sc = max(sc, p["json"]["n"] * 3)
        if p.get("sitemap"):
            sc = max(sc, min(p["sitemap"]["locs"], 60))
        if sc > best_score:
            best, best_score = i, sc
    return {"id": t["id"], "name": t["name"], "group": t["group"], "pages": pages, "best": best, "best_score": best_score}


def main():
    t0 = time.time()
    out = {"companies": [], "exist": {}}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for r in ex.map(lambda t: _safe(probe_company, t), CFG["targets"]):
            out["companies"].append(r)
    out["sec_pages"] = round(time.time() - t0)
    # bekannte Leser
    ex_out = {"join": [], "personio": [], "workday": []}
    cache = {}
    for cid, name, slug in CFG["exist"]["join"]:
        n_err = len(scan.ERRORS)
        try:
            jobs = scan.scan_join({"id": cid, "name": name, "slug": slug}, cache)
        except Exception as e:  # noqa: BLE001
            jobs = None
            scan.ERRORS.append(f"{type(e).__name__}: {e}")
        ex_out["join"].append({"id": cid, "slug": slug, "n": None if jobs is None else len(jobs),
                               "titles": [j.get("title", "")[:60] for j in (jobs or [])[:4]], "errors": scan.ERRORS[n_err:]})
        time.sleep(1.0)
    for cid, name, feed in CFG["exist"]["personio"]:
        n_err = len(scan.ERRORS)
        try:
            jobs = scan.scan_personio({"id": cid, "name": name, "feed": feed})
        except Exception as e:  # noqa: BLE001
            jobs = None
            scan.ERRORS.append(f"{type(e).__name__}: {e}")
        ex_out["personio"].append({"id": cid, "feed": feed, "n": None if jobs is None else len(jobs),
                                   "titles": [j.get("title", "")[:60] for j in (jobs or [])[:4]],
                                   "offices": sorted({str(j.get("location") or "")[:30] for j in (jobs or [])})[:8], "errors": scan.ERRORS[n_err:]})
    for e in CFG["exist"]["workday"]:
        n_err = len(scan.ERRORS)
        try:
            jobs = scan.scan_workday(e)
        except Exception as ex2:  # noqa: BLE001
            jobs = None
            scan.ERRORS.append(f"{type(ex2).__name__}: {ex2}")
        ex_out["workday"].append({"id": e["id"], "n": None if jobs is None else len(jobs),
                                  "titles": [j.get("title", "")[:60] for j in (jobs or [])[:6]],
                                  "locs": sorted({str(j.get("location") or "")[:30] for j in (jobs or [])})[:10], "errors": scan.ERRORS[n_err:]})
    out["exist"] = ex_out
    out["sec"] = round(time.time() - t0)
    json.dump(out, open(os.path.join(OUT, "pages.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    print("fertig", out["sec"], "s;", len(out["companies"]), "Firmen")


def _safe(fn, t):
    try:
        return fn(t)
    except Exception as e:  # noqa: BLE001
        return {"id": t["id"], "name": t["name"], "group": t["group"], "pages": [], "best": None, "crash": f"{type(e).__name__}: {str(e)[:200]}"}


if __name__ == "__main__":
    main()
