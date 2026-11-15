#!/usr/bin/env python3
"""Probe 7: Workday-Facetten von GETEC im Rohformat."""
import json, os
import scan
OUT = os.path.join(scan.DATA, "probe"); os.makedirs(OUT, exist_ok=True)
url = "https://getec.wd103.myworkdayjobs.com/wday/cxs/getec/External_Careers/jobs"
d = scan.http_post_json(url, {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}) or {}
res = {"facets_raw": json.dumps(d.get("facets"), ensure_ascii=False)[:6000], "total": d.get("total")}
import re
fac = scan.workday_facets(url, re.compile("^Deutschland$", re.I))
res["applied"] = fac
for variant in (fac, {"locationMainGroup": [v for vs in fac.values() for v in vs]}):
    r = scan.http_post_json(url, {"appliedFacets": variant, "limit": 20, "offset": 0, "searchText": ""}) or {}
    res.setdefault("tries", []).append({"facets": variant, "total": r.get("total"), "n": len(r.get("jobPostings") or []),
                                        "sample": [[p.get("title"), p.get("locationsText")] for p in (r.get("jobPostings") or [])[:8]]})
res["errors"] = scan.ERRORS
json.dump(res, open(os.path.join(OUT, "p7.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("fertig")
