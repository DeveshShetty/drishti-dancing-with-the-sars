#!/usr/bin/env python3
"""Convert NISAR L2 GCOV (HDF5) and Sentinel-1 RTC (GeoTIFF) scenes into 8-bit PNGs + manifest.json for the viewer.

Example (both sensors on one grid):
  python nisar_to_viewer.py --scenes "nisar_data/*.h5" "s1_data/*/*.tif" --bbox 160.05 54.45 160.50 54.75 \
      --pixel 30 --out ../viewer/data --title "Krasheninnikov volcano, Kamchatka" --event 2025-08-03 --event-label "Eruption begins"

NISAR scenes must share track + direction (override: --allow-mixed). Sentinel-1 files should come from ONE relative orbit
(the converter cannot see orbit in the file name, so that is on you). Sensors are never mixed in one comparison.
--average D1,D2 adds a pass that is the mean of those dates (less speckle noise; same radar mode recommended).
Encoding: byte 0 = nodata, byte = 1 + round((dB + 30) / 35 * 254), dB range [-30, 5].
"""
import argparse, collections, glob, json, os, re, sys
import numpy as np
from PIL import Image
from pyproj import Transformer

DB_LO, DB_HI = -30.0, 5.0
FREQ = "/science/LSAR/GCOV/grids/frequencyA"
NISAR_NAME = re.compile(r"NISAR_L2_(?P<prod>PR|UR)_GCOV_(?P<cycle>\d+)_(?P<track>\d+)_(?P<dir>[AD])_(?P<frame>\d+)_(?P<mode>\d+)_(?P<pol>[A-Z]+)_[A-Z]_(?P<start>\d{8})T\d{6}")
# GeoTIFFs written by scripts/nisar_bangladesh_erosion.py: <YYYYMMDD>T<HHMM>_HH.tif / _HV.tif, plus summary.csv
NISAR_TIF = re.compile(r"^(?P<date>\d{8})T(?P<hm>\d{4})_(?P<pol>HH|HV)\.tif$", re.I)
S1_NAME = re.compile(r"^(?P<sat>S1[ABCD])_.*?_(?P<date>\d{8})T\d{6}.*?_(?P<pol>VV|VH|HH|HV)\.tif$", re.I)
SENSOR_NISAR, SENSOR_S1 = "NISAR L-band", "Sentinel-1 C-band"

def iso(d8):
    return f"{d8[0:4]}-{d8[4:6]}-{d8[6:8]}"

def corners(bbox, epsg):
    t = Transformer.from_crs(4326, epsg, always_xy=True)
    pts = [t.transform(lo, la) for lo, la in [(bbox[0], bbox[1]), (bbox[0], bbox[3]), (bbox[2], bbox[1]), (bbox[2], bbox[3])]]
    return min(p[0] for p in pts), max(p[0] for p in pts), min(p[1] for p in pts), max(p[1] for p in pts)

def load_nisar(path, extent):
    import h5py
    with h5py.File(path, "r") as f:
        g = f[FREQ]
        x = g["xCoordinates"][()]; y = g["yCoordinates"][()]
        p = g["projection"]
        epsg = int(np.array(p.attrs["epsg_code"]).ravel()[0]) if "epsg_code" in p.attrs else int(np.array(p[()]).ravel()[0])
        x0, x1, y0, y1 = extent(epsg)
        ix = np.where((x >= x0) & (x <= x1))[0]; iy = np.where((y >= y0) & (y <= y1))[0]
        if len(ix) == 0 or len(iy) == 0:
            return None
        sx = slice(ix.min(), ix.max() + 1); sy = slice(iy.min(), iy.max() + 1)
        co = g["HHHH"][sy, sx].astype("float32") if "HHHH" in g else None
        cr = g["HVHV"][sy, sx].astype("float32") if "HVHV" in g else None
        if co is None and "VVVV" in g:
            co = g["VVVV"][sy, sx].astype("float32")
        return dict(co=co, cross=cr, x=x[sx], y=y[sy], epsg=epsg, pols=("HH", "HV"), names=[k for k in g.keys() if k.endswith(("HH", "HV", "VV", "VH"))])

def load_tif(paths, extent, scale):
    import rasterio
    from rasterio.windows import from_bounds
    out = {}
    for pol, pth in paths.items():
        with rasterio.open(pth) as ds:
            epsg = ds.crs.to_epsg()
            x0, x1, y0, y1 = extent(epsg)
            win = from_bounds(x0, y0, x1, y1, ds.transform).round_offsets().round_lengths()
            win = win.intersection(rasterio.windows.Window(0, 0, ds.width, ds.height))
            if win.width < 2 or win.height < 2:
                return None
            arr = ds.read(1, window=win).astype("float32")
            wt = ds.window_transform(win)
            x = wt.c + (np.arange(arr.shape[1]) + 0.5) * wt.a
            y = wt.f + (np.arange(arr.shape[0]) + 0.5) * wt.e
            nod = ds.nodata
        if nod is not None and np.isfinite(nod):
            arr = np.where(arr == nod, np.nan, arr)
        if scale == "amplitude":
            arr = arr ** 2
        elif scale == "db":
            arr = 10 ** (arr / 10.0)
        out[pol.upper()] = (arr, x, y, epsg)
    pols = list(out.keys())
    co_pol = "VV" if "VV" in out else ("HH" if "HH" in out else pols[0])
    cr_pol = "VH" if "VH" in out else ("HV" if "HV" in out else None)
    arr, x, y, epsg = out[co_pol]
    cr = None
    if cr_pol and out[cr_pol][0].shape == arr.shape:
        cr = out[cr_pol][0]
    return dict(co=arr, cross=cr, x=x, y=y, epsg=epsg, pols=(co_pol, cr_pol or ""), names=pols)

def encode_db(db):
    b = 1 + np.rint((db - DB_LO) / (DB_HI - DB_LO) * 254)
    b = np.nan_to_num(b, nan=0.0, posinf=0.0, neginf=0.0)
    b = np.clip(b, 1, 255).astype("uint8")
    b[~np.isfinite(db)] = 0
    return b

class Grid:
    def __init__(self, epsg, X0, X1, Y0, Y1, W, H):
        self.epsg, self.X0, self.X1, self.Y0, self.Y1, self.W, self.H = epsg, X0, X1, Y0, Y1, W, H
        self.Xc = X0 + (np.arange(W) + 0.5) * (X1 - X0) / W
        self.Yc = Y1 - (np.arange(H) + 0.5) * (Y1 - Y0) / H

    def to_db(self, arr, x, y, epsg, pixel):
        """Resample a file window to the output grid, return dB (nan = nodata) and a description."""
        W, H = self.W, self.H
        native = abs(float(x[1] - x[0])) if len(x) > 1 else pixel
        if epsg != self.epsg:
            t = Transformer.from_crs(self.epsg, epsg, always_xy=True)
            XX, YY = np.meshgrid(self.Xc, self.Yc)
            xf, yf = t.transform(XX, YY)
            fx = (xf - x[0]) / (x[1] - x[0]); fy = (yf - y[0]) / (y[1] - y[0])
            ok = (fx >= -0.5) & (fx < len(x) - 0.5) & (fy >= -0.5) & (fy < len(y) - 0.5)
            v = arr[np.clip(np.rint(fy).astype(int), 0, len(y) - 1), np.clip(np.rint(fx).astype(int), 0, len(x) - 1)]
            v = np.where(ok & np.isfinite(v) & (v > 0), v, np.nan)
            desc = f"native {native:g} m, reprojected EPSG:{epsg}->{self.epsg}, nearest-neighbour"
        elif pixel < native * 0.99:
            fx = (self.Xc - x[0]) / (x[1] - x[0]); fy = (self.Yc - y[0]) / (y[1] - y[0])
            ok = (fy[:, None] >= -0.5) & (fy[:, None] < len(y) - 0.5) & (fx[None, :] >= -0.5) & (fx[None, :] < len(x) - 0.5)
            v = arr[np.clip(np.rint(fy).astype(int), 0, len(y) - 1)[:, None], np.clip(np.rint(fx).astype(int), 0, len(x) - 1)[None, :]]
            v = np.where(ok & np.isfinite(v) & (v > 0), v, np.nan)
            desc = f"native {native:g} m, nearest-neighbour (output finer than source)"
        else:
            cx = np.clip(((x - self.X0) / (self.X1 - self.X0) * W).astype(int), -1, W)
            cy = np.clip(((self.Y1 - y) / (self.Y1 - self.Y0) * H).astype(int), -1, H)
            okx = (cx >= 0) & (cx < W); oky = (cy >= 0) & (cy < H)
            a2 = np.where(np.isfinite(arr) & (arr > 0), arr, 0.0)
            m = oky[:, None] & okx[None, :]
            flat = (cy[:, None] * W + cx[None, :])[m]
            sums = np.bincount(flat, weights=a2[m], minlength=W * H)
            cnt = np.bincount(flat, weights=(a2[m] > 0).astype("float64"), minlength=W * H)
            with np.errstate(divide="ignore", invalid="ignore"):
                v = np.where(cnt > 0, sums / np.maximum(cnt, 1), np.nan).reshape(H, W)
            desc = f"native {native:g} m, block average"
        with np.errstate(divide="ignore", invalid="ignore"):
            return 10 * np.log10(v), desc

def nisar_tif_meta(folder, tag):
    """Row of summary.csv (written next to the GeoTIFFs) whose datetime matches tag 'YYYYMMDDTHHMM'."""
    import csv
    p = os.path.join(folder, "summary.csv")
    if not os.path.exists(p):
        return {}
    for r in csv.DictReader(open(p, newline="")):
        if r.get("datetime_utc", "").replace("-", "").replace(":", "")[:13] == tag:
            return r
    return {}

def collect(patterns, s1_scale):
    files = []
    for s in patterns:
        files += sorted(glob.glob(s, recursive=True)) or [s]
    items, s1, nt = [], collections.defaultdict(dict), collections.defaultdict(dict)
    for p in files:
        b = os.path.basename(p)
        m = NISAR_NAME.search(b)
        if m and p.lower().endswith((".h5", ".hdf5")):
            d = m.groupdict(); s_ = d["start"]
            items.append(dict(sensor=SENSOR_NISAR, kind="nisar", date=iso(s_), path=p, prod=d["prod"], track=int(d["track"]), dir=d["dir"], frame=int(d["frame"]), mode=d["mode"], scene=b))
            continue
        m = NISAR_TIF.match(b)
        if m:
            nt[(os.path.dirname(os.path.abspath(p)), m.group("date") + "T" + m.group("hm"))][m.group("pol").upper()] = p
            continue
        m = S1_NAME.match(b)
        if m:
            key = (m.group("date"), re.sub(r"_(VV|VH|HH|HV)\.tif$", "", b, flags=re.I))
            s1[key][m.group("pol").upper()] = p
            continue
        print(f"skip (name not recognised): {p}")
    for (folder, tag), pols in sorted(nt.items()):           # NISAR subsets as GeoTIFF + summary.csv metadata
        meta = nisar_tif_meta(folder, tag)
        src = meta.get("source", "")
        n = NISAR_NAME.search(src)
        d = n.groupdict() if n else {}
        items.append(dict(sensor=SENSOR_NISAR, kind="nisar_tif", date=iso(tag[:8]), paths=pols, scale="power",
                          prod=d.get("prod", "PR"), track=int(meta.get("track") or d.get("track") or 0),
                          dir=(meta.get("direction") or d.get("dir") or "?")[:1].upper(),
                          frame=int(meta.get("frame") or d.get("frame") or 0), mode=d.get("mode", ""),
                          scene=src[:-3] if src.endswith(".h5") else (src or tag)))
    for (d8, stem), pols in sorted(s1.items()):
        items.append(dict(sensor=SENSOR_S1, kind="s1", date=iso(d8), paths=pols, prod="RTC", scene=stem, scale=s1_scale))
    return items

def fmt_day(iso_date):
    y, m, d = iso_date.split("-")
    return f"{int(d)} {'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split()[int(m) - 1]}"

def make_average(spec, frames, dbs, out):
    """Mean of several passes in linear power (not dB), kept only where every pass has data. Averaging N passes
    lowers speckle noise by about sqrt(N) without blurring, at the cost of mixing their dates."""
    dates = sorted({d.strip() for d in spec.split(",") if d.strip()})
    if len(dates) < 2:
        sys.exit(f"--average needs at least two dates: {spec!r}")
    sensor_pick = None
    if ":" in spec.split(",")[0]:                      # optional sensor prefix: "s1:2026-09-05,2026-09-17" or "nisar:..."
        sensor_pick, first = spec.split(",")[0].split(":", 1)
        sensor_pick = {"s1": SENSOR_S1, "nisar": SENSOR_NISAR}.get(sensor_pick.lower())
        if sensor_pick is None:
            sys.exit(f"--average: unknown sensor prefix in {spec!r} (use s1: or nisar:)")
        dates = sorted({first.strip(), *[d.strip() for d in spec.split(",")[1:] if d.strip()]})
    def pick(d):
        c = [f for f in frames if f["date"] == d and not f.get("average_of") and (sensor_pick is None or f["sensor"].startswith(sensor_pick))]
        if len(c) > 1:
            sys.exit(f"--average: {d} exists for several sensors; prefix the dates with s1: or nisar:")
        return c[0] if c else None
    src = [pick(d) for d in dates]
    missing = [d for d, f in zip(dates, src) if f is None]
    if missing:
        sys.exit(f"--average: no pass on {', '.join(missing)} (available: {', '.join(sorted({f['date'] for f in frames}))})")
    sensors = {f["sensor"] for f in src}
    if len(sensors) > 1:
        sys.exit(f"--average mixes sensors {sorted(sensors)}; average passes of one sensor only.")
    modes = {f.get("mode") for f in src}
    if len(modes) > 1:
        print(f"  WARNING: --average {spec}: passes have different radar modes {sorted(map(str, modes))}; "
              "their noise and resolution differ, so the mean is less clean.")
    sensor = sensors.pop()
    rec = {"date": dates[-1], "sensor": sensor, "product": src[-1]["product"],
           "scene": "mean of " + ", ".join(f["scene"] for f in src), "average_of": dates,
           "label": "Avg " + " + ".join(fmt_day(d) for d in dates), "short": "avg",
           "note": f"mean of {len(dates)}", "pols": src[-1]["pols"]}
    for k in ("track", "direction", "frame", "mode"):
        if k in src[-1]:
            rec[k] = src[-1][k]
    prefix = "n" if sensor.startswith(SENSOR_NISAR) else "s1"
    for key in ("hh", "hv"):
        if not all((sensor, d, key) in dbs for d in dates):
            continue
        with np.errstate(invalid="ignore", over="ignore"):
            lin = np.stack([10 ** (dbs[(sensor, d, key)] / 10) for d in dates])
            mean = lin.mean(axis=0)                       # nan wherever any pass lacks data
            db = 10 * np.log10(mean)
        fn = f"{prefix}_avg_{'_'.join(dates)}_{key}.png"
        Image.fromarray(encode_db(db), "L").save(os.path.join(out, fn))
        rec[key] = fn
    if "hh" not in rec:
        sys.exit(f"--average {spec}: no co-pol image to average")
    print(f"  average of {', '.join(dates)} ok  -> '{rec['label']}'")
    return rec

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenes", nargs="+", required=True)
    ap.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    ap.add_argument("--pixel", type=float, default=30.0, help="output pixel size in metres")
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="Study area")
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--event", default=None, help="event date YYYY-MM-DD")
    ap.add_argument("--event-label", default="")
    ap.add_argument("--default-pair", nargs=2, metavar=("BEFORE", "AFTER"),
                    help="dates (YYYY-MM-DD) the viewer opens on, e.g. pre- vs post-monsoon when there is no single event")
    ap.add_argument("--s1-scale", default="power", choices=["power", "amplitude", "db"], help="units of the Sentinel-1 GeoTIFFs (HyP3 default: power)")
    ap.add_argument("--allow-mixed", action="store_true")
    ap.add_argument("--orbit-label", action="append", default=[], metavar="GLOB=TEXT",
                    help="add TEXT to the sensor name of scenes whose path matches GLOB, e.g. \"s1_asc/*=ascending\". "
                         "Each sensor name is compared only with itself, so use this to keep ascending and descending "
                         "Sentinel-1 orbits apart (their file names do not say which is which).")
    ap.add_argument("--average", action="append", default=[], metavar="DATE,DATE[,...]",
                    help="also add a pass that is the mean (in linear power) of these dates of one sensor, e.g. a quieter "
                         "'before' image: --average 2026-08-28,2026-09-21. Repeatable. Use passes of the same radar mode.")
    a = ap.parse_args()

    items = collect(a.scenes, a.s1_scale)
    for spec in a.orbit_label:
        if "=" not in spec:
            sys.exit(f"--orbit-label needs GLOB=TEXT, got {spec!r}")
        pat, text = spec.rsplit("=", 1)
        hits = {os.path.normcase(os.path.abspath(x)) for x in glob.glob(pat, recursive=True)}
        def match(i):
            ps = [i["path"]] if "path" in i else list(i["paths"].values())
            return any(os.path.normcase(os.path.abspath(x)) in hits for x in ps)
        n = 0
        for i in items:
            if match(i):
                i["sensor"] = f"{i['sensor']} · {text.strip()}"; n += 1
        print(f"orbit label '{text.strip()}': {n} passes")
    if not items:
        sys.exit("No NISAR GCOV (.h5) or Sentinel-1 RTC (.tif) files recognised.")
    nis = [i for i in items if i["kind"] in ("nisar", "nisar_tif")]
    keys = {(i["track"], i["dir"]) for i in nis}
    if len(keys) > 1 and not a.allow_mixed:
        sys.exit(f"Mixed NISAR track/direction {sorted(keys)}. Compare one track at a time (or pass --allow-mixed).")
    by = {}
    for i in items:                       # one scene per (sensor, date); prefer NISAR PR over UR
        k = (i["sensor"], i["date"])
        if k not in by or (by[k].get("prod") == "UR" and i["prod"] == "PR"):
            by[k] = i
    items = sorted(by.values(), key=lambda i: (i["sensor"], i["date"]))

    grid = None
    pad = max(2 * a.pixel, 100.0)            # read a little beyond the area so edge pixels can be filled

    def extent(epsg):
        """Area to read from a file in its own CRS: the output grid's outline once the grid exists (its corners can
        stick out past the bbox corners when the file is in another projection), else the bbox. Plus a margin."""
        if grid is None or epsg == grid.epsg:
            x0, x1, y0, y1 = corners(a.bbox, epsg) if grid is None else (grid.X0, grid.X1, grid.Y0, grid.Y1)
        else:
            s = np.linspace(0, 1, 41)
            ex = np.concatenate([grid.X0 + s * (grid.X1 - grid.X0), np.full(41, grid.X1), grid.X1 - s * (grid.X1 - grid.X0), np.full(41, grid.X0)])
            ey = np.concatenate([np.full(41, grid.Y0), grid.Y0 + s * (grid.Y1 - grid.Y0), np.full(41, grid.Y1), grid.Y1 - s * (grid.Y1 - grid.Y0)])
            fx, fy = Transformer.from_crs(grid.epsg, epsg, always_xy=True).transform(ex, ey)
            x0, x1, y0, y1 = fx.min(), fx.max(), fy.min(), fy.max()
        return x0 - pad, x1 + pad, y0 - pad, y1 + pad

    def load(i):
        return load_nisar(i["path"], extent) if i["kind"] == "nisar" else load_tif(i["paths"], extent, i["scale"])

    first = None
    for i in items:
        first = load(i)
        if first is not None:
            break
    if first is None:
        sys.exit("bbox does not overlap any scene.")
    epsg = first["epsg"]
    X0, X1, Y0, Y1 = corners(a.bbox, epsg)
    W = max(8, int(round((X1 - X0) / a.pixel))); H = max(8, int(round((Y1 - Y0) / a.pixel)))
    grid = Grid(epsg, X0, X1, Y0, Y1, W, H)
    print(f"output grid {W}x{H} at {a.pixel} m (EPSG:{epsg})")

    os.makedirs(a.out, exist_ok=True)
    frames, dbs = [], {}
    for i in items:
        r = load(i)
        tag = f"{i['sensor'].split()[0]} {i['date']}"
        if r is None:
            print(f"  {tag}: no overlap, skipped"); continue
        rec = {"date": i["date"], "sensor": i["sensor"], "product": i["prod"], "scene": i["scene"], "pols": [p for p in r["pols"]]}
        if i["kind"] in ("nisar", "nisar_tif"):
            rec.update(track=i["track"], direction="ascending" if i["dir"] == "A" else "descending", frame=i["frame"], mode=i["mode"])
        prefix = "n" if i["kind"] in ("nisar", "nisar_tif") else "s1"
        for band, key in (("co", "hh"), ("cross", "hv")):
            arr = r[band]
            if arr is None:
                continue
            db, desc = grid.to_db(arr, r["x"], r["y"], r["epsg"], a.pixel)
            fn = f"{prefix}_{i['date']}_{key}.png"
            Image.fromarray(encode_db(db), "L").save(os.path.join(a.out, fn))
            rec[key] = fn
            if a.average:
                dbs[(i["sensor"], i["date"], key)] = db
        frames.append(rec)
        print(f"  {tag} ok  ({desc})")

    for spec in a.average:
        frames.append(make_average(spec, frames, dbs, a.out))
    # chronological per sensor; an averaged pass sorts after the single pass of the same (latest) date
    frames.sort(key=lambda f: (f["sensor"], f["date"], 1 if f.get("average_of") else 0))

    tr = Transformer.from_crs(epsg, 4326, always_xy=True)
    w_, s_ = tr.transform(X0, Y0); e_, n_ = tr.transform(X1, Y1)
    sensors = sorted({f["sensor"] for f in frames})
    man = {"title": a.title, "subtitle": a.subtitle or " + ".join(sensors),
           "bbox": [round(w_, 5), round(s_, 5), round(e_, 5), round(n_, 5)], "width": W, "height": H,
           "pixel_size_m": a.pixel, "db_range": [DB_LO, DB_HI], "sensors": sensors, "frames": frames,
           # exact lon/lat of the grid's outer corners (TL, TR, BR, BL). The grid is UTM, i.e. rotated against north,
           # so the viewer interpolates between these four rather than treating bbox as a lon/lat rectangle.
           "crs": f"EPSG:{epsg}",
           "corners": [[round(v, 6) for v in tr.transform(x, y)] for x, y in ((X0, Y1), (X1, Y1), (X1, Y0), (X0, Y0))],
           "story": [{"title": "Your reading of the data", "body": "Describe what changed, where, and the physical driver you propose."}]}
    if a.event:
        man["event"] = {"date": a.event, "label": a.event_label}
    if a.default_pair:
        man["default_pair"] = list(a.default_pair)
    json.dump(man, open(os.path.join(a.out, "manifest.json"), "w"), indent=2)
    print(f"wrote {len(frames)} passes ({', '.join(sensors)}) + manifest.json to {a.out}")

if __name__ == "__main__":
    main()
