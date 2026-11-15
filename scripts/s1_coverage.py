"""Sentinel-1 coverage check over the study box. Run locally (needs internet, no login for search).
  python s1_coverage.py
Writes s1_scenes.csv. Prints per-year counts, track/direction groups, and passes around the 26 Aug 2026 event.
"""
import csv, collections, datetime as dt
import asf_search as asf

BOX = (85.15, 27.95, 85.37, 28.25)            # W S E N, same as the NISAR viewer box
EVENT = dt.date(2026, 8, 26)
WKT = "POLYGON((%s %s,%s %s,%s %s,%s %s,%s %s))" % (BOX[0],BOX[1],BOX[2],BOX[1],BOX[2],BOX[3],BOX[0],BOX[3],BOX[0],BOX[1])
END = dt.date.today().isoformat()

def run(label, **kw):
    print("\n=== %s ===" % label)
    try:
        res = asf.geo_search(intersectsWith=WKT, start="2014-10-01", end=END, maxResults=4000, **kw)
    except Exception as e:
        print("search failed:", e); return []
    rows = []
    for r in res:
        p = r.properties
        rows.append(dict(source=label, date=p["startTime"][:10], platform=p.get("platform"), direction=p.get("flightDirection"),
                         orbit=p.get("pathNumber"), level=p.get("processingLevel"), scene=p.get("sceneName"), url=p.get("url")))
    print("total granules:", len(rows))
    by_year = collections.Counter(r["date"][:4] for r in rows)
    print("per year:", dict(sorted(by_year.items())))
    groups = collections.defaultdict(set)
    for r in rows: groups[(r["direction"], r["orbit"])].add(r["date"])
    for k, d in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print("  %s orbit %s: %d dates, first %s last %s" % (k[0], k[1], len(d), min(d), max(d)))
    near = sorted({(r["date"], r["direction"], r["orbit"], r["platform"]) for r in rows
                   if abs((dt.date.fromisoformat(r["date"]) - EVENT).days) <= 45})
    print("passes within 45 days of the event:")
    for d in near: print("  ", d[0], d[1], "orbit", d[2], d[3], "(%+d d)" % (dt.date.fromisoformat(d[0]) - EVENT).days)
    return rows

allrows = []
allrows += run("OPERA_RTC", dataset=asf.DATASET.OPERA_S1, processingLevel="RTC")
allrows += run("S1_GRD", platform=[asf.PLATFORM.SENTINEL1], processingLevel="GRD_HD")
with open("s1_scenes.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["source","date","platform","direction","orbit","level","scene","url"])
    w.writeheader(); w.writerows(allrows)
print("\nwrote s1_scenes.csv with", len(allrows), "rows")
