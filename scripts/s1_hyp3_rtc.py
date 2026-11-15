"""Order terrain-corrected (RTC) Sentinel-1 images from ASF HyP3 (free with an Earthdata login) and download them.
 1. python scripts/volcano_coverage.py            (makes volcano_scenes.csv)
 2. python scripts/s1_hyp3_rtc.py --csv volcano_scenes.csv --track 162 --direction DESCENDING --start 2025-07-20 --end 2026-11-01 --dry-run
 3. same command without --dry-run: submits jobs, waits, downloads to ./s1_data and unzips
Rerunning is safe: scenes already in --out are skipped, finished HyP3 jobs are reused, downloads resume.
Use ONE track and direction so images are comparable."""
import argparse, csv
import hyp3_sdk as sdk
ap = argparse.ArgumentParser()
ap.add_argument("--csv", required=True); ap.add_argument("--track", type=int, required=True)
ap.add_argument("--direction", required=True, choices=["ASCENDING", "DESCENDING"])
ap.add_argument("--start", required=True); ap.add_argument("--end", required=True)
ap.add_argument("--resolution", type=int, default=30, choices=[10, 20, 30]); ap.add_argument("--out", default="s1_data")
ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--name", default="krasheninnikov", help="HyP3 job name")
a = ap.parse_args()
rows = [r for r in csv.DictReader(open(a.csv)) if r["source"] == "S1" and r["direction"].upper() == a.direction
        and str(r["track"]) == str(a.track) and a.start <= r["date"] <= a.end]
names = sorted({r["scene"].rsplit(".", 1)[0] for r in rows})
print("%d Sentinel-1 scenes selected" % len(names))
for n in names: print("  ", n)
if a.dry_run or not names:
    raise SystemExit("dry run done" if names else "nothing matched: check --track/--direction/dates against the CSV")
import glob, os, sys, zipfile, requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _dl import fetch

def key(name):                    # S1A_IW_GRDH_1SDV_20250726T193217_... and S1A_IW_20250726T193217_DVP_RTC30... -> S1A_20250726T193217
    parts = os.path.basename(name.rstrip("/\\")).split("_")
    return parts[0] + "_" + next(x for x in parts if len(x) == 15 and x[8] == "T")

def have(n):                      # already extracted: folder with both the VV and VH images (a cut-off unzip lacks one)
    return any(glob.glob(os.path.join(d, "*_VV.tif")) and glob.glob(os.path.join(d, "*_VH.tif"))
               for d in glob.glob(os.path.join(a.out, "*")) if os.path.isdir(d) and key(d) == key(n))

todo = [n for n in names if not have(n)]
print("%d already in %s, %d to get" % (len(names) - len(todo), a.out, len(todo)))
if not todo:
    raise SystemExit("nothing to do")
try:
    hyp3 = sdk.HyP3()             # uses _netrc if present
except Exception:
    hyp3 = sdk.HyP3(prompt=True)  # asks for your Earthdata username/password once
# Reuse finished or still-running jobs with the same settings (outputs are kept ~14 days), so a rerun does not
# use credits again; e.g. if this script is stopped while waiting, running it again picks the same jobs back up.
def same_settings(j):
    p = j.job_parameters
    return (int(p.get("resolution", 30)) == a.resolution and p.get("scale", "power") == "power"
            and p.get("radiometry", "gamma0") == "gamma0")
done = {}
for j in hyp3.find_jobs(job_type="RTC_GAMMA"):
    g = j.job_parameters.get("granules", [None])[0]
    if g in todo and g not in done and not j.failed() and not (j.succeeded() and j.expired()) and same_settings(j):
        done[g] = j
batch = sdk.Batch([done[n] for n in todo if n in done])
for n in todo:
    if n not in done:
        batch += hyp3.submit_rtc_job(n, name=a.name, resolution=a.resolution, scale="power", radiometry="gamma0")
print("reusing %d finished jobs, submitted %d new" % (len(done), len(todo) - len(done)))
if not batch.complete():
    print("waiting for HyP3 (this can take a while)...")
    batch = hyp3.watch(batch)
ok = batch.filter_jobs(succeeded=True, running=False, failed=False)
print("succeeded:", len(ok), "of", len(batch))
os.makedirs(a.out, exist_ok=True)
session, failed = requests.Session(), []
for j in ok:
    f = j.files[0]
    z = os.path.join(a.out, f["filename"])
    try:
        fetch(session, f["url"], z)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(a.out)
        print("  unzipped", f["filename"])
    except (RuntimeError, zipfile.BadZipFile) as e:
        print("FAILED", f["filename"], e); failed.append(f["filename"])
print("downloaded to", a.out, "| failed: %s (rerun to resume)" % failed if failed else "")
sys.exit(1 if failed or len(ok) < len(batch) else 0)
