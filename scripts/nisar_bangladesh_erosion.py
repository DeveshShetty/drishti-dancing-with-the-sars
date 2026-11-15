#!/usr/bin/env python3
"""
NISAR GCOV riverbank-erosion time series -- Bangladesh (Padma at Sadarpur, Faridpur by default).

What it does
  1. Searches NISAR L2 GCOV (PROVISIONAL, calibrated) over your AOI with asf_search.
  2. Keeps ONE track/frame that fully covers the AOI (never mixes viewing geometries).
  3. Picks one scene per month (or every scene with --every-scene).
  4. STREAMS only the AOI window from each HDF5 (no multi-GB downloads).
  5. Per date writes:   <date>_HH.tif, <date>_HV.tif   gamma0 POWER, 20 m (your simulator format)
                        <date>_landmask.tif            1 = land, 0 = water
                        <date>_shoreline.geojson       land/water boundary (lon/lat)
  6. Before vs after (first vs last date, or --before / --after) writes:
                        change_erosion.tif             1 = ERODED (land -> water), 2 = accreted (water -> land)
                        change_erosion.geojson         eroded / accreted polygons with hectares
                        change_composite.tif           RGB: R = before, G/B = after  (red = got darker/water,
                                                       cyan = got brighter/land)
  7. summary.csv   (date, land km2, water threshold) + erosion totals printed at the end.

Install
    pip install --upgrade asf_search h5py fsspec aiohttp rasterio pyproj shapely numpy

Earthdata login: EARTHDATA_TOKEN if set (https://urs.earthdata.nasa.gov -> Generate Token), otherwise your
_netrc / .netrc Earthdata login via earthaccess (asked once if neither exists).

Drishti viewer: the <date>_HH.tif / _HV.tif files and summary.csv load directly in the viewer:
    python pipeline/nisar_to_viewer.py --scenes "nisar_bd_out/*_H?.tif" --bbox W S E N --pixel 20 --out viewer/data_bangladesh
Rerunning skips dates already written. --list-only shows what would be processed.

Run
    python nisar_bangladesh_erosion.py                                  # Padma, Sadarpur (default)
    python nisar_bangladesh_erosion.py --bbox W S E N                   # any CEGIS 2026 hotspot
    python nisar_bangladesh_erosion.py --every-scene                    # all ~12-day scenes for a chart
    python nisar_bangladesh_erosion.py --before 2026-06 --after 2026-11 # choose comparison months

Tip: compare PRE-monsoon (June) with POST-monsoon (Oct/Nov). Mid-monsoon images show flood water,
not erosion. Land that is still water after the flood recedes is land the river has taken.
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

try:
    import asf_search as asf
    import fsspec
    import h5py
    import rasterio
    from pyproj import Transformer
    from rasterio.features import shapes
    from rasterio.transform import from_origin
    from rasterio.warp import Resampling, reproject
    from shapely.geometry import box, mapping, shape
    from shapely.ops import transform as shp_transform
    from shapely.ops import unary_union
except ImportError as e:
    sys.exit(f"Missing package: {e.name}\n  pip install --upgrade asf_search h5py fsspec aiohttp "
             "rasterio pyproj shapely numpy")

GRID = "/science/LSAR/GCOV/grids/frequencyA"
CRID_RE = re.compile(r"(NISAR_L2_\w\w_GCOV(?:_[^_]+){9})_([A-Z])(\d{5})")

# Padma right bank around Sadarpur, Faridpur (CEGIS 2026 erosion hotspot).
# Approximate -- check against the CEGIS 2026 index map and adjust.
DEFAULT_BBOX = [89.90, 23.35, 90.20, 23.65]


# =============================================================================== args
def parse_args():
    p = argparse.ArgumentParser(description="NISAR riverbank erosion, Bangladesh")
    p.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"), default=DEFAULT_BBOX)
    p.add_argument("--start", default="2026-06-17", help="provisional NISAR data begins 2026-06-17")
    p.add_argument("--end", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    p.add_argument("--interval-months", type=int, default=1, help="one scene per N months")
    p.add_argument("--every-scene", action="store_true", help="use every acquisition (~12 days)")
    p.add_argument("--include-beta", action="store_true", help="add pre-calibration BETA products")
    p.add_argument("--pixel", type=float, default=20.0, help="output pixel (m); 0 = native")
    p.add_argument("--track", type=int, help="force relative orbit")
    p.add_argument("--frame", type=int, help="force frame")
    p.add_argument("--before", help="YYYY-MM for the 'before' image (default: first date)")
    p.add_argument("--after", help="YYYY-MM for the 'after' image (default: last date)")
    p.add_argument("--min-patch-ha", type=float, default=1.0, help="ignore change patches smaller than this")
    p.add_argument("--out", default="nisar_bd_out")
    p.add_argument("--mode", help="keep only this NISAR radar mode, e.g. 2005. Mixing modes creates false change "
                                   "(different resolution and noise), so pick one for comparisons.")
    p.add_argument("--list-only", action="store_true", help="search and list the chosen scenes, process nothing")
    return p.parse_args()


# =============================================================================== search
def search_scenes(args):
    aoi = box(*args.bbox)
    products = []
    for maturity in ["PROVISIONAL"] + (["BETA"] if args.include_beta else []):
        kw = dict(dataset="NISAR", processingLevel="GCOV", intersectsWith=aoi.wkt,
                  start=args.start, end=args.end, maxResults=2000)
        try:
            res = asf.search(dataMaturity=maturity, **kw)
        except Exception as e:
            print(f"  (dataMaturity filter not supported: {e}) -- searching without it")
            res = asf.search(**kw)
        for r in res:
            r._maturity = maturity
        products.extend(res)
        print(f"  {maturity}: {len(res)} GCOV results")
    return aoi, products


def h5_url(product):
    pr = product.properties
    urls = [pr.get("url")] + list(pr.get("additionalUrls") or [])
    urls = [u for u in urls if u and u.endswith(".h5") and "QA_STATS" not in u]
    return urls[0] if urls else None


def latest_versions(products):
    best = {}
    for p in products:
        url = h5_url(p)
        if not url:
            continue
        m = CRID_RE.search(url)
        key, ver = (m.group(1), int(m.group(3))) if m else (url, 0)
        if key not in best or ver > best[key][0]:
            best[key] = (ver, p, url)
    return [(p, url) for _, p, url in best.values()]


def pick_track(aoi, items, args):
    groups = defaultdict(list)
    for p, url in items:
        if not shape(p.geometry).contains(aoi):
            continue
        pr = p.properties
        groups[(pr.get("pathNumber"), pr.get("frameNumber"), pr.get("flightDirection"))].append((p, url))
    groups = {k: v for k, v in groups.items()
              if (args.track is None or k[0] == args.track) and (args.frame is None or k[1] == args.frame)}
    if not groups:
        sys.exit("No single track/frame fully covers the AOI. Shrink --bbox or set --track/--frame.")
    print("  Tracks fully covering AOI  (track, frame, direction): scenes")
    for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"    {k}: {len(v)}")
    key = max(groups, key=lambda k: len(groups[k]))
    print(f"  -> using {key}")
    return key, groups[key]


def acq_time(p):
    return datetime.fromisoformat(p.properties["startTime"].replace("Z", "+00:00"))


MODE_RE = re.compile(r"NISAR_L2_\w\w_GCOV_\d+_\d+_[AD]_\d+_(\d{4})_([A-Z]{4})_[A-Z]_\d{8}T\d{6}_\d{8}T\d{6}_\w+?_([A-Z])_([A-Z])_")


def scene_mode(url):
    """(mode, polarisation, frame-coverage flag) from the file name, e.g. ('2005', 'QPDH', 'F')."""
    m = MODE_RE.search(url.rsplit("/", 1)[-1])
    return (m.group(1), m.group(2), m.group(4)) if m else ("", "", "")


def one_per_date(scenes, args):
    """Several products can share an acquisition (different modes / partial frames). Keep one per date: the requested
    mode if given, then products with HH and HV (QP.., DH..), then full frames."""
    by = defaultdict(list)
    for s in scenes:
        mode, pol, cov = scene_mode(s[1])
        if args.mode and mode != args.mode:
            continue
        if pol and not pol.startswith(("QP", "DH", "SH")):   # e.g. DVNA: VV only, no HH
            continue
        by[acq_time(s[0]).date()].append(s)
    out = []
    for d in sorted(by):
        out.append(min(by[d], key=lambda s: (0 if scene_mode(s[1])[1].startswith(("QP", "DH")) else 1,
                                             0 if scene_mode(s[1])[2] == "F" else 1)))
    return out


def select_scenes(scenes, args):
    scenes = one_per_date(sorted(scenes, key=lambda s: acq_time(s[0])), args)
    if not scenes:
        sys.exit("No scenes left after the mode / polarisation filter.")
    if args.every_scene:
        return scenes
    t0 = acq_time(scenes[0][0])
    windows = defaultdict(list)
    for s in scenes:
        t = acq_time(s[0])
        windows[((t.year - t0.year) * 12 + t.month - t0.month) // args.interval_months].append(s)
    chosen = []
    for k in sorted(windows):
        ts = [acq_time(s[0]) for s in windows[k]]
        mid = min(ts) + (max(ts) - min(ts)) / 2
        chosen.append(min(windows[k], key=lambda s: abs(acq_time(s[0]) - mid)))
    # make sure explicitly requested before/after months are included
    for ym in (args.before, args.after):
        if ym and not any(acq_time(s[0]).strftime("%Y-%m") == ym for s in chosen):
            cand = [s for s in scenes if acq_time(s[0]).strftime("%Y-%m") == ym]
            if cand:
                chosen.append(cand[0])
    return sorted(chosen, key=lambda s: acq_time(s[0]))


# =============================================================================== read
def https_fs(token):
    """Authenticated HTTPS filesystem: bearer token if given, else the earthaccess login (_netrc, env, or prompt)."""
    if token:
        return fsspec.filesystem("https", headers={"Authorization": f"Bearer {token}"},
                                 client_kwargs={"trust_env": True})
    import earthaccess
    earthaccess.login(persist=True)
    return earthaccess.get_fsspec_https_session()


def read_subset(url, bbox, fs):
    """Stream only the AOI window of HHHH / HVHV from a remote GCOV HDF5."""
    with fs.open(url, "rb", block_size=8 * 1024 * 1024, cache_type="blockcache") as fobj, \
            h5py.File(fobj, "r") as f:
        g = f[GRID]
        epsg = int(g["projection"][()])
        x, y = g["xCoordinates"][:], g["yCoordinates"][:]
        tr = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
        W, S, E, N = bbox
        xs, ys = tr.transform([W, E, W, E], [S, S, N, N])
        c0, c1 = np.searchsorted(x, min(xs)), np.searchsorted(x, max(xs))
        ydesc = y[0] > y[-1]
        if ydesc:
            r0, r1 = np.searchsorted(-y, -max(ys)), np.searchsorted(-y, -min(ys))
        else:
            r0, r1 = np.searchsorted(y, min(ys)), np.searchsorted(y, max(ys))
        if c1 - c0 < 2 or r1 - r0 < 2:
            raise ValueError("AOI does not overlap this scene")
        layers = {pol: g[term][r0:r1, c0:c1].astype("float32")
                  for pol, term in (("HH", "HHHH"), ("HV", "HVHV")) if term in g}
        dx, dy = float(x[1] - x[0]), float(abs(y[1] - y[0]))
        if not ydesc:
            layers = {k: np.flipud(v) for k, v in layers.items()}
            top = y[r1 - 1] + dy / 2
        else:
            top = y[r0] + dy / 2
        transform = from_origin(x[c0] - dx / 2, top, dx, dy)
    return layers, transform, epsg, dx


def block_average(arr, f):
    if f <= 1:
        return arr
    h, w = arr.shape[0] // f * f, arr.shape[1] // f * f
    return np.nanmean(arr[:h, :w].reshape(h // f, f, w // f, f), axis=(1, 3))


# =============================================================================== analysis
def to_db(power):
    with np.errstate(divide="ignore", invalid="ignore"):
        return 10 * np.log10(np.where(power > 0, power, np.nan))


def otsu(values, bins=256):
    v = values[np.isfinite(values)]
    hist, edges = np.histogram(v, bins=bins)
    c = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(hist)
    w1 = w0[-1] - w0
    s = np.cumsum(hist * c)
    m0 = s / np.maximum(w0, 1)
    m1 = (s[-1] - s) / np.maximum(w1, 1)
    return float(c[np.argmax(w0 * w1 * (m0 - m1) ** 2)])


def land_mask(hh_power):
    """L-band HH: open water is dark. Per-scene Otsu threshold, clamped to a sensible range."""
    db = to_db(hh_power)
    thr = min(max(otsu(db), -24.0), -12.0)
    valid = np.isfinite(db)
    return ((db > thr) & valid), valid, thr


def write_tif(path, arr, transform, epsg, nodata=None):
    count = 1 if arr.ndim == 2 else arr.shape[0]
    with rasterio.open(path, "w", driver="GTiff", height=arr.shape[-2], width=arr.shape[-1],
                       count=count, dtype=arr.dtype, crs=f"EPSG:{epsg}", transform=transform,
                       nodata=nodata, compress="deflate") as dst:
        dst.write(arr if arr.ndim == 3 else arr[np.newaxis])


def polygons(mask, transform, epsg, min_area_m2):
    to_ll = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    polys = [shape(g) for g, v in shapes(mask.astype("uint8"), mask=mask, transform=transform) if v == 1]
    polys = [p for p in polys if p.area >= min_area_m2]
    if not polys:
        return None, 0.0
    merged = unary_union(polys)
    return shp_transform(lambda xx, yy, z=None: to_ll.transform(xx, yy), merged), merged.area


def align_to(src_arr, src_tf, dst_shape, dst_tf, epsg, resampling):
    out = np.full(dst_shape, np.nan, dtype="float32")
    reproject(src_arr.astype("float32"), out, src_transform=src_tf, src_crs=f"EPSG:{epsg}",
              dst_transform=dst_tf, dst_crs=f"EPSG:{epsg}", resampling=resampling,
              src_nodata=np.nan, dst_nodata=np.nan)
    return out


def stretch(db, lo=-25.0, hi=0.0):
    return (np.clip((db - lo) / (hi - lo), 0, 1) * 254 + 1).astype("uint8")  # 0 reserved for nodata


# =============================================================================== main
def main():
    args = parse_args()
    token = os.environ.get("EARTHDATA_TOKEN")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"AOI {args.bbox}\nSearching NISAR GCOV ...")
    aoi, products = search_scenes(args)
    items = latest_versions(products)
    if not items:
        sys.exit("No GCOV products found. Widen dates or check the AOI.")
    key, scenes = pick_track(aoi, items, args)
    chosen = select_scenes(scenes, args)
    print(f"Processing {len(chosen)} scenes")
    for p, url in chosen:
        print(f"    {acq_time(p):%Y-%m-%d %H:%M}  {p._maturity:11s} {url.rsplit('/', 1)[-1]}")
    if args.list_only:
        return
    fs = https_fs(token)

    dates, rows = [], []
    for p, url in chosen:
        t = acq_time(p)
        tag = t.strftime("%Y%m%dT%H%M")
        have = [out / f"{tag}_{pol}.tif" for pol in ("HH", "HV")]
        if all(h.exists() for h in have):                       # written by an earlier run: reuse
            layers = {}
            for pol, h in zip(("HH", "HV"), have):
                with rasterio.open(h) as ds:
                    layers[pol], tf, epsg = ds.read(1).astype("float32"), ds.transform, ds.crs.to_epsg()
            print(f"  {tag} [{p._maturity}] already written, reusing")
        else:
            print(f"  {tag} [{p._maturity}] streaming AOI ...")
            layers = None
            for attempt in range(1, 5):                         # connections to Earthdata sometimes reset
                try:
                    layers, tf, epsg, dx = read_subset(url, args.bbox, fs)
                    break
                except ValueError as e:
                    print(f"    skipped: {e}")
                    break
                except Exception as e:
                    print(f"    attempt {attempt} failed ({type(e).__name__}: {str(e)[:120]}); retrying")
                    import time
                    time.sleep(10 * attempt)
            if layers is None:
                print("    skipped after retries")
                continue
            f = int(round(args.pixel / dx)) if args.pixel else 1
            if f > 1:
                layers = {k: block_average(v, f) for k, v in layers.items()}
                tf = tf * tf.scale(f, f)
            layers = {k: np.where(v > 0, v, np.nan).astype("float32") for k, v in layers.items()}
            for pol, arr in layers.items():
                write_tif(out / f"{tag}_{pol}.tif", arr, tf, epsg, nodata=np.nan)
        if "HH" not in layers:
            print("    skipped: no HH channel")
            continue
        land, valid, thr = land_mask(layers["HH"])
        write_tif(out / f"{tag}_landmask.tif", np.where(valid, land, 255).astype("uint8"), tf, epsg, nodata=255)
        geom, area = polygons(land, tf, epsg, min_area_m2=args.min_patch_ha * 1e4)
        if geom is not None:
            with open(out / f"{tag}_shoreline.geojson", "w") as fh:
                json.dump({"type": "FeatureCollection", "features": [{"type": "Feature",
                           "properties": {"date": t.date().isoformat(), "land_km2": round(area / 1e6, 3)},
                           "geometry": mapping(geom.boundary)}]}, fh)
        dates.append({"t": t, "tag": tag, "hh": layers["HH"], "land": land, "valid": valid,
                      "tf": tf, "epsg": epsg})
        rows.append({"datetime_utc": t.isoformat(), "maturity": p._maturity, "track": key[0],
                     "frame": key[1], "direction": key[2], "hh_water_threshold_db": round(thr, 2),
                     "land_km2": round(area / 1e6, 3), "source": url.rsplit("/", 1)[-1]})
        print(f"    land {area / 1e6:.2f} km2   threshold {thr:.1f} dB")

    with open(out / "summary.csv", "w", newline="") as fh:
        if rows:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    if len(dates) < 2:
        print("Fewer than 2 usable dates -- no before/after change map.")
        return

    # ---------------------------------------------------------------- before vs after
    def pick(ym, default):
        if ym:
            for d in dates:
                if d["t"].strftime("%Y-%m") == ym:
                    return d
            print(f"  no processed scene in {ym}; using default")
        return default

    b, a = pick(args.before, dates[0]), pick(args.after, dates[-1])
    epsg, tf, shp = b["epsg"], b["tf"], b["hh"].shape
    a_land = align_to(a["land"].astype("float32"), a["tf"], shp, tf, epsg, Resampling.nearest)
    a_valid = align_to(a["valid"].astype("float32"), a["tf"], shp, tf, epsg, Resampling.nearest)
    a_hh = align_to(a["hh"], a["tf"], shp, tf, epsg, Resampling.bilinear)
    both = b["valid"] & (a_valid == 1)

    eroded = both & b["land"] & (a_land == 0)
    accreted = both & ~b["land"] & (a_land == 1)
    change = np.zeros(shp, dtype="uint8")
    change[eroded], change[accreted] = 1, 2
    write_tif(out / "change_erosion.tif", change, tf, epsg, nodata=None)

    rgb = np.stack([stretch(to_db(b["hh"])), stretch(to_db(a_hh)), stretch(to_db(a_hh))])
    rgb[:, ~both] = 0
    write_tif(out / "change_composite.tif", rgb, tf, epsg, nodata=0)

    feats, min_m2 = [], args.min_patch_ha * 1e4
    totals = {}
    for name, m in (("eroded", eroded), ("accreted", accreted)):
        g, area = polygons(m, tf, epsg, min_m2)
        totals[name] = area / 1e4
        if g is not None:
            feats.append({"type": "Feature", "properties": {"type": name, "hectares": round(area / 1e4, 1),
                          "before": b["t"].date().isoformat(), "after": a["t"].date().isoformat()},
                          "geometry": mapping(g)})
    with open(out / "change_erosion.geojson", "w") as fh:
        json.dump({"type": "FeatureCollection", "features": feats}, fh)

    print(f"\nBefore {b['t'].date()}  ->  After {a['t'].date()}")
    print(f"  ERODED (land -> water): {totals['eroded']:.1f} ha")
    print(f"  accreted (water -> land): {totals['accreted']:.1f} ha")
    print(f"Done -> {out.resolve()}")


if __name__ == "__main__":
    main()
