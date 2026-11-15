"""End-to-end test of pipeline/nisar_to_viewer.py on SYNTHETIC scenes with known, planted changes.

  python tests/test_synthetic.py          (or: pytest tests/test_synthetic.py)

Builds fake NISAR GCOV .h5 files (UTM 45N, 10 m) and Sentinel-1 RTC GeoTIFFs (UTM 44N, 20 m, so the converter
must reproject), runs the converter, decodes the PNGs and checks:
  - background dB values survive the round trip (within PNG quantisation)
  - a disk that brightens by +6 dB is found with the right area and position, in BOTH sensors
  - a rectangle that darkens by -20 dB ("flood") is found with the right area
  - a nodata strip becomes byte 0 only in the pass that has it
  - *_ls_map.tif helper files are skipped, PR is preferred over UR for the same date
  - mixed NISAR tracks and a bbox with no overlap are rejected
Nothing is written into the project: everything goes to a temp folder (pass --keep DIR to keep the output).
"""
import json, os, subprocess, sys, tempfile
import numpy as np
from PIL import Image
from pyproj import Transformer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPE = os.path.join(ROOT, "pipeline", "nisar_to_viewer.py")
DB_LO, DB_HI = -30.0, 5.0
PIX = 30.0
EPSG_N, EPSG_S1 = 32645, 32644                     # NISAR in zone 45, Sentinel-1 in zone 44 (forces reprojection)
BBOX = (85.280, 28.080, 85.320, 28.120)            # W S E N
TO_N = Transformer.from_crs(4326, EPSG_N, always_xy=True)

# Planted features, defined in NISAR (EPSG:32645) metres
CX, CY = TO_N.transform(85.300, 28.100)            # disk centre
R = 600.0                                          # disk radius (m) -> area 1.131 km2
FX0, FY0 = TO_N.transform(85.283, 28.083)          # flood rectangle 900 m x 600 m, SW corner
FX1, FY1 = FX0 + 900.0, FY0 + 600.0
BG_HH, BG_HV = -8.0, -15.0
DISK_DHH, DISK_DHV = 6.0, 4.0
FLOOD_HH, FLOOD_HV = -28.0, -27.0
NODATA_W = 300.0                                   # nodata strip width (m) at the bbox west edge, NISAR pass 1 only

def truth_db(X, Y, disk, flood):
    """HH and HV dB at NISAR-frame coordinates X, Y."""
    hh = np.full(X.shape, BG_HH); hv = np.full(X.shape, BG_HV)
    if disk:
        m = (X - CX) ** 2 + (Y - CY) ** 2 <= R ** 2
        hh[m] += DISK_DHH; hv[m] += DISK_DHV
    if flood:
        m = (X >= FX0) & (X <= FX1) & (Y >= FY0) & (Y <= FY1)
        hh[m] = FLOOD_HH; hv[m] = FLOOD_HV
    return hh, hv

def p(db):
    return (10 ** (db / 10)).astype("float32")

def write_nisar(path, disk, flood, nodata_strip=False, track=48):
    import h5py
    x0, x1, y0, y1 = CX - 4000, CX + 4000, CY - 4000, CY + 4000
    x = np.arange(x0, x1, 10.0) + 5; y = np.arange(y1, y0, -10.0) - 5      # y descending, like real GCOV
    X, Y = np.meshgrid(x, y)
    hh, hv = truth_db(X, Y, disk, flood)
    HH, HV = p(hh), p(hv)
    if nodata_strip:
        wx, _ = TO_N.transform(BBOX[0], BBOX[1])
        wx = min(wx, TO_N.transform(BBOX[0], BBOX[3])[0])
        HH[:, (x >= wx) & (x < wx + NODATA_W)] = np.nan
        HV[:, (x >= wx) & (x < wx + NODATA_W)] = np.nan
    with h5py.File(path, "w") as f:
        g = f.create_group("/science/LSAR/GCOV/grids/frequencyA")
        g["xCoordinates"] = x; g["yCoordinates"] = y
        pr = g.create_dataset("projection", data=np.uint32(EPSG_N)); pr.attrs["epsg_code"] = np.int32(EPSG_N)
        g["HHHH"] = HH; g["HVHV"] = HV

def write_s1(folder, stem, disk, flood):
    import rasterio
    from rasterio.transform import from_origin
    ox, oy = Transformer.from_crs(EPSG_N, EPSG_S1, always_xy=True).transform(CX, CY)
    x0, y1, n, res = ox - 5000, oy + 5000, 500, 20.0
    xs = x0 + (np.arange(n) + 0.5) * res; ys = y1 - (np.arange(n) + 0.5) * res
    XS, YS = np.meshgrid(xs, ys)
    XN, YN = Transformer.from_crs(EPSG_S1, EPSG_N, always_xy=True).transform(XS, YS)
    hh, hv = truth_db(XN, YN, disk, flood)
    os.makedirs(os.path.join(folder, stem), exist_ok=True)
    prof = dict(driver="GTiff", width=n, height=n, count=1, dtype="float32", crs=f"EPSG:{EPSG_S1}",
                transform=from_origin(x0, y1, res, res), nodata=0.0)
    for pol, db in (("VV", hh), ("VH", hv)):
        with rasterio.open(os.path.join(folder, stem, f"{stem}_{pol}.tif"), "w", **prof) as ds:
            ds.write(p(db), 1)
    with rasterio.open(os.path.join(folder, stem, f"{stem}_ls_map.tif"), "w", **{**prof, "dtype": "uint8", "nodata": None}) as ds:
        ds.write(np.ones((n, n), "uint8"), 1)

def nisar_name(date, prod="PR", track=48, cycle=10):
    return f"NISAR_L2_{prod}_GCOV_{cycle:03d}_{track:03d}_D_074_4005_DHDH_A_{date}T120000_{date}T120030_P05023_N_F_J_001.h5"

def run(args):
    return subprocess.run([sys.executable, PIPE] + args, capture_output=True, text=True)

def decode(path):
    b = np.asarray(Image.open(path)).astype(float)
    db = (b - 1) / 254 * (DB_HI - DB_LO) + DB_LO
    db[b == 0] = np.nan
    return db

def centroid(mask):
    yy, xx = np.nonzero(mask); return xx.mean(), yy.mean()

def build(tmp):
    nd, sd = os.path.join(tmp, "nisar"), os.path.join(tmp, "s1")
    os.makedirs(nd); os.makedirs(sd)
    write_nisar(os.path.join(nd, nisar_name("20260701")), disk=False, flood=False, nodata_strip=True)
    write_nisar(os.path.join(nd, nisar_name("20260713")), disk=True, flood=False)
    write_nisar(os.path.join(nd, nisar_name("20260725")), disk=True, flood=True)
    # same date as a PR file but UR with a wrong (no change) signal: must lose to PR
    write_nisar(os.path.join(nd, nisar_name("20260725", prod="UR")), disk=False, flood=False)
    write_s1(sd, "S1A_IW_20260702T120000_DVP_RTC30_G_gpuned_AAAA", disk=False, flood=False)
    write_s1(sd, "S1A_IW_20260726T120000_DVP_RTC30_G_gpuned_BBBB", disk=True, flood=True)
    return nd, sd

def main(keep=None):
    tmp = keep or tempfile.mkdtemp(prefix="nisar_synth_")
    os.makedirs(tmp, exist_ok=True)
    nd, sd = build(tmp)
    out = os.path.join(tmp, "viewer_data")
    r = run(["--scenes", os.path.join(nd, "*.h5"), os.path.join(sd, "*", "*.tif"), "--bbox", *map(str, BBOX),
             "--pixel", str(PIX), "--out", out, "--title", "SYNTHETIC test", "--event", "2026-07-10", "--event-label", "planted"])
    print(r.stdout.strip())
    results = []
    def check(name, ok, detail=""):
        results.append(ok); print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))

    check("converter exits 0", r.returncode == 0, r.stderr.strip()[-300:])
    if r.returncode != 0:
        return 1
    m = json.load(open(os.path.join(out, "manifest.json")))
    fr = {(f["sensor"].split()[0], f["date"]): f for f in m["frames"]}
    check("5 passes (3 NISAR, 2 Sentinel-1)", len(m["frames"]) == 5, f"{sorted(fr)}")
    check("both sensors listed", m["sensors"] == ["NISAR L-band", "Sentinel-1 C-band"], str(m["sensors"]))
    check("event in manifest", m.get("event", {}).get("date") == "2026-07-10")
    check("ls_map helper skipped", "ls_map.tif" in r.stdout and "skip" in r.stdout)
    check("PR preferred over UR", fr[("NISAR", "2026-07-25")]["product"] == "PR", fr[("NISAR", "2026-07-25")]["scene"][:20])

    img = lambda s, d, k: decode(os.path.join(out, fr[(s, d)][k]))
    px_km2 = (PIX / 1000) ** 2
    disk_km2, flood_km2 = np.pi * R ** 2 / 1e6, 0.9 * 0.6
    q = 0.5 * (DB_HI - DB_LO) / 254 + 1e-6          # PNG quantisation half-step (0.069 dB)

    a = img("NISAR", "2026-07-13", "hh")
    bg = a[np.isfinite(a)]
    check("NISAR background HH = -8 dB", abs(np.median(bg) - BG_HH) <= q, f"{np.median(bg):.3f}")
    hv = img("NISAR", "2026-07-13", "hv")
    check("NISAR background HV = -15 dB", abs(np.nanmedian(hv) - BG_HV) <= q, f"{np.nanmedian(hv):.3f}")

    n1, n2, n3 = img("NISAR", "2026-07-01", "hh"), a, img("NISAR", "2026-07-25", "hh")
    strip = np.isnan(n1)
    exp_cols = NODATA_W / PIX
    cols = strip.all(axis=0).sum()
    check("nodata strip -> byte 0 in pass 1", abs(cols - exp_cols) <= 1, f"{cols} cols, expected ~{exp_cols:.0f}")
    check("no nodata in passes 2 and 3", np.isfinite(n2).all() and np.isfinite(n3).all())

    d = n2 - n1
    brighter = (d > DISK_DHH / 2) & np.isfinite(d)
    area = brighter.sum() * px_km2
    check("NISAR disk area (+6 dB) within 5%", abs(area - disk_km2) / disk_km2 < 0.05, f"{area:.3f} vs {disk_km2:.3f} km2")
    inside = d[brighter]
    check("NISAR disk change = +6 dB", abs(np.median(inside) - DISK_DHH) <= 2 * q, f"{np.median(inside):.3f}")
    # expected disk centre on the output grid
    gx0, gx1, gy0, gy1 = [], [], [], []
    for lo, la in [(BBOX[0], BBOX[1]), (BBOX[0], BBOX[3]), (BBOX[2], BBOX[1]), (BBOX[2], BBOX[3])]:
        X, Y = TO_N.transform(lo, la); gx0.append(X); gy1.append(Y)
    X0, Y1 = min(gx0), max(gy1)
    ex, ey = (CX - X0) / PIX - 0.5, (Y1 - CY) / PIX - 0.5
    cx, cy = centroid(brighter)
    check("NISAR disk in the right place (<1 px)", np.hypot(cx - ex, cy - ey) < 1, f"({cx:.1f},{cy:.1f}) vs ({ex:.1f},{ey:.1f})")
    darker = ((n3 - n2) < -10) & np.isfinite(n3 - n2)
    fa = darker.sum() * px_km2
    check("NISAR flood area (-20 dB) within 5%", abs(fa - flood_km2) / flood_km2 < 0.05, f"{fa:.3f} vs {flood_km2:.3f} km2")
    stable = np.isfinite(n2) & np.isfinite(n1) & ~brighter
    edge = np.abs(d[stable]) > 0.5
    check("no false change outside planted areas (<0.5% px)", edge.mean() < 0.005, f"{edge.mean()*100:.2f}% of stable px")

    s1a, s1b = img("Sentinel-1", "2026-07-02", "hh"), img("Sentinel-1", "2026-07-26", "hh")
    check("Sentinel-1 VV stored as co-pol, background -8 dB", abs(np.nanmedian(s1a) - BG_HH) <= q, f"{np.nanmedian(s1a):.3f}")
    check("Sentinel-1 covers whole bbox", np.isfinite(s1a).all() and np.isfinite(s1b).all())
    ds = s1b - s1a
    sb = (ds > DISK_DHH / 2) & np.isfinite(ds)
    sa = sb.sum() * px_km2
    check("Sentinel-1 disk area within 5% (after reprojection)", abs(sa - disk_km2) / disk_km2 < 0.05, f"{sa:.3f} vs {disk_km2:.3f} km2")
    sx, sy = centroid(sb)
    check("Sentinel-1 disk aligned with NISAR disk (<1 px)", np.hypot(sx - cx, sy - cy) < 1, f"({sx:.1f},{sy:.1f}) vs ({cx:.1f},{cy:.1f})")
    sf = ((ds < -10) & np.isfinite(ds)).sum() * px_km2
    check("Sentinel-1 flood area within 8%", abs(sf - flood_km2) / flood_km2 < 0.08, f"{sf:.3f} vs {flood_km2:.3f} km2")

    # failure modes
    mixed = os.path.join(tmp, "mixed"); os.makedirs(mixed, exist_ok=True)
    write_nisar(os.path.join(mixed, nisar_name("20260701", track=48)), False, False)
    write_nisar(os.path.join(mixed, nisar_name("20260702", track=49)), False, False)
    r2 = run(["--scenes", os.path.join(mixed, "*.h5"), "--bbox", *map(str, BBOX), "--out", os.path.join(tmp, "o2")])
    check("mixed tracks rejected", r2.returncode != 0 and "Mixed" in (r2.stderr + r2.stdout), (r2.stderr + r2.stdout).strip()[-120:])
    r3 = run(["--scenes", os.path.join(nd, "*.h5"), "--bbox", "10", "10", "10.1", "10.1", "--out", os.path.join(tmp, "o3")])
    check("bbox with no overlap rejected cleanly", r3.returncode != 0 and "overlap" in (r3.stderr + r3.stdout), (r3.stderr + r3.stdout).strip()[-160:])

    print(f"\n{sum(results)}/{len(results)} checks passed. Output: {out}")
    return 0 if all(results) else 1

def test_synthetic():                                 # pytest entry point
    assert main() == 0

if __name__ == "__main__":
    k = sys.argv[sys.argv.index("--keep") + 1] if "--keep" in sys.argv else None
    sys.exit(main(k))
