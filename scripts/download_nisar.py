"""Download NISAR GCOV scenes listed in a coverage CSV (from volcano_coverage.py).
  python scripts/download_nisar.py --csv volcano_scenes.csv --track 40 --direction DESCENDING --dry-run
  python scripts/download_nisar.py --csv volcano_scenes.csv --track 40 --direction DESCENDING --start 2026-06-17 --end 2026-11-01
Keep one track + direction. Files are large; --dry-run lists them first. Needs a free Earthdata login (asked once).
Downloads resume: if it stops (network drop, restart, Ctrl+C), run the same command again."""
import argparse, csv, os
ap = argparse.ArgumentParser()
ap.add_argument("--csv", required=True); ap.add_argument("--track", type=int, required=True)
ap.add_argument("--direction", required=True, choices=["ASCENDING", "DESCENDING"])
ap.add_argument("--frame", type=int); ap.add_argument("--start", default="0000-00-00"); ap.add_argument("--end", default="9999-99-99")
ap.add_argument("--out", default="nisar_data"); ap.add_argument("--dry-run", action="store_true")
a = ap.parse_args()
rows = [r for r in csv.DictReader(open(a.csv)) if r["source"] == "NISAR" and str(r["track"]) == str(a.track)
        and r["direction"].upper() == a.direction and (a.frame is None or str(r["frame"]) == str(a.frame)) and a.start <= r["date"] <= a.end]
best = {}
for r in rows:                                   # one file per date, prefer PR over UR
    if r["date"] not in best or (best[r["date"]]["product"] == "UR" and r["product"] == "PR"):
        best[r["date"]] = r
sel = [best[d] for d in sorted(best)]
print(len(sel), "scenes:")
for r in sel: print("  ", r["date"], r["product"], "frame", r["frame"], r["mode"], r["maturity"])
if a.dry_run or not sel:
    raise SystemExit("dry run done" if sel else "nothing matched: check --track/--direction/dates against the CSV")
import sys, earthaccess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _dl import fetch
earthaccess.login(persist=True)   # tries env vars, then _netrc/.netrc, then asks
session = earthaccess.get_requests_https_session()
os.makedirs(a.out, exist_ok=True)
failed = []
for r in sel:                     # resumable + retrying; rerun the same command to continue after any stop
    try:
        fetch(session, r["url"], os.path.join(a.out, r["url"].rsplit("/", 1)[1]))
    except RuntimeError as e:
        print("FAILED", r["date"], e); failed.append(r["date"])
print("done ->", a.out, "| failed: %s (rerun to resume)" % failed if failed else "")
sys.exit(1 if failed else 0)
