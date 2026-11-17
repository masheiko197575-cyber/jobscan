"""probe 9: JOIN-Neuausschreibungen und softgarden-Status (Prüfung der Morgenaufgabe 17.11.2026). Wenige Anfragen,
eine alle 3 s, dieselben öffentlichen Seiten, die der Scanner liest. Ergebnis: data/probe/probe9.json"""
import json, os, re, time, requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) jobscan/1.0", "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}
URLS = [
    "https://join.com/companies/egs-plan",
    "https://join.com/companies/egs-plan/16519627-kaufmaennische-projektassistenz-m-w-d-voll-oder-teilzeit",
    "https://join.com/companies/egs-plan/16401986-kaufmaennische-projektassistenz-m-w-d-voll-oder-teilzeit",
    "https://join.com/companies/junizenergy/16874449-projektleiter-m-w-d-batteriegrossspeicher-bess",
    "https://join.com/companies/junizenergy/16152446-lead-electrical-engineer-batteriegrossspeicher-bess-m-w-d",
    "https://wemag.softgarden.io/job/51621143/Koordinator-Netzanschl%C3%BCsse-EE-Anlagen-m-w-d?l=de",
    "https://wemag.softgarden.io/de/vacancies",
]
out = []
for u in URLS:
    try:
        r = requests.get(u, headers=UA, timeout=30, allow_redirects=True)
        txt = r.text or ""
        m = re.search(r"<title[^>]*>(.*?)</title>", txt, re.S | re.I)
        rec = {"url": u, "status": r.status_code, "final": r.url, "len": len(txt),
               "server": r.headers.get("Server"), "cf": r.headers.get("Cf-Mitigated"),
               "title": m.group(1).strip()[:160] if m else None,
               "closed_marker": bool(re.search(r"nicht mehr verfügbar|nicht mehr aktiv|no longer available|abgelaufen|expired|"
                                               r"bereits besetzt|existiert nicht", txt, re.I))}
        if u.rstrip("/").count("/") == 4 and "join.com/companies/" in u:
            rec["jobs"] = sorted(set(re.findall(r"/companies/[a-z0-9-]+/(\d{6,9})-([a-z0-9-]+)", txt)))[:40]
        if "softgarden" in u:
            rec["netz"] = re.findall(r"[^<>\"]{0,60}Netzanschl[^<>\"]{0,60}", txt)[:5]
    except Exception as ex:  # noqa: BLE001
        rec = {"url": u, "error": f"{type(ex).__name__}: {str(ex)[:200]}"}
    out.append(rec)
    print(json.dumps(rec, ensure_ascii=False)[:700], flush=True)
    time.sleep(3)
os.makedirs("data/probe", exist_ok=True)
json.dump(out, open("data/probe/probe9.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
