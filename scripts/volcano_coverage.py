"""Coverage check for Krasheninnikov volcano (Kamchatka): NISAR GCOV + Sentinel-1 GRD.
Run locally (internet, no login needed for search):  python scripts/volcano_coverage.py
Writes volcano_scenes.csv. Edit BOX if you want a different area (W S E N)."""
import csv, re, collections, datetime as dt
import asf_search as asf

BOX = (160.05, 54.45, 160.50, 54.75)       # approx. Krasheninnikov, check on a map
END = dt.date.today().isoformat()
WKT = "POLYGON((%s %s,%s %s,%s %s,%s %s,%s %s))" % (BOX[0],BOX[1],BOX[2],BOX[1],BOX[2],BOX[3],BOX[0],BOX[3],BOX[0],BOX[1])
NAME = re.compile(r"NISAR_L2_(PR|UR)_GCOV_(\d+)_(\d+)_([AD])_(\d+)_(\d+)_([A-Z]+)_[A-Z]_(\d{8})T")

def parse(r, label):
    p = r.properties; name = p.get("sceneName") or ""
    d = dict(source=label, date=p["startTime"][:10], direction=p.get("flightDirection"), track=p.get("pathNumber"),
             frame=p.get("frameNumber"), product="", mode="", scene=name, url=p.get("url"), maturity=p.get("dataMaturity",""))
    m = NAME.search(name)
    if m:
        d.update(product=m.group(1), track=int(m.group(3)), direction={"A":"ASCENDING","D":"DESCENDING"}[m.group(4)], frame=int(m.group(5)), mode=m.group(6)+"/"+m.group(7))
    return d

def show(rows, title):
    print("\n=== %s: %d scenes ===" % (title, len(rows)))
    g = collections.defaultdict(list)
    for r in rows: g[(r["direction"], r["track"], r["frame"])].append(r["date"])
    for k, ds in sorted(g.items(), key=lambda kv: -len(set(kv[1]))):
        ds = sorted(set(ds)); print("%s track %s frame %s: %d dates" % (k[0], k[1], k[2], len(ds)))
        print("   ", " ".join(ds))

rows = []
try:
    res = asf.geo_search(dataset=asf.DATASET.NISAR, processingLevel="GCOV", intersectsWith=WKT, start="2025-10-01", end=END, maxResults=2000)
    n = [parse(r, "NISAR") for r in res]; rows += n; show(n, "NISAR GCOV")
except Exception as e:
    print("NISAR search failed:", e)
try:
    res = asf.geo_search(platform=[asf.PLATFORM.SENTINEL1], processingLevel="GRD_HD", intersectsWith=WKT, start="2025-06-01", end=END, maxResults=2000)
    s = [parse(r, "S1") for r in res]; rows += s; show(s, "Sentinel-1 GRD_HD (since Jun 2025)")
except Exception as e:
    print("Sentinel-1 search failed:", e)
with open("volcano_scenes.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["source","date","direction","track","frame","product","mode","maturity","scene","url"]); w.writeheader(); w.writerows(rows)
print("\nwrote volcano_scenes.csv (%d rows)" % len(rows))
