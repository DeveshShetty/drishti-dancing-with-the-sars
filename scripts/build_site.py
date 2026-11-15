"""Build the public website (dist/) from viewer/ for Firebase Hosting or any static host.

    python scripts/build_site.py                 # public build, no Google Maps key (Google basemap button hidden)
    python scripts/build_site.py --with-maps-key # include viewer/config.js (restrict that key to your domain first!)
    firebase deploy --only hosting               # publishes dist/ (see firebase.json, docs/DEPLOY.md)

viewer/ is never modified. Left out of dist/: the local Google Maps key file (unless --with-maps-key), old dataset
copies, the saved Esri-only viewer, the local dev server and the key template. The radar PNGs are copied unchanged:
their grey levels are measured dB values, and a lossless WebP version saved only ~4 %.
"""
import argparse
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "viewer")
DST = os.path.join(ROOT, "dist")
SKIP_TOP = {"config.js", "config.example.js", "serve.py", "index_esri.html", "data_gorkha_old", "data_nepal_old",
            "__pycache__"}
PUBLIC_CONFIG = ("/* Public build: no Google Maps key, so the Google basemap button is hidden.\n"
                 "   Esri satellite, Topo, Streets and 3D basemaps work without a key. */\n"
                 "window.NCE_CONFIG = { googleMapsKey: \"\" };\n")


def build(with_maps_key=False):
    if os.path.exists(DST):
        shutil.rmtree(DST)

    def ignore(d, names):
        top = os.path.abspath(d) == os.path.abspath(SRC)
        return [n for n in names if (top and n in SKIP_TOP) or n == "__pycache__" or n.endswith("_old")]

    shutil.copytree(SRC, DST, ignore=ignore)
    key_src = os.path.join(SRC, "config.js")
    if with_maps_key:
        if not os.path.exists(key_src):
            raise SystemExit("--with-maps-key: viewer/config.js not found (copy viewer/config.example.js and add a key)")
        shutil.copy2(key_src, os.path.join(DST, "config.js"))
    else:
        with open(os.path.join(DST, "config.js"), "w", encoding="utf-8") as fh:
            fh.write(PUBLIC_CONFIG)
    # every dataset in the menu must be present, and must not reference missing files
    with open(os.path.join(DST, "datasets.json"), encoding="utf-8") as fh:
        menu = json.load(fh)
    missing = []
    for ds in menu:
        man_path = os.path.join(DST, ds["dir"], "manifest.json")
        if not os.path.exists(man_path):
            missing.append(f"{ds['dir']}/manifest.json")
            continue
        with open(man_path, encoding="utf-8") as fh:
            man = json.load(fh)
        for f in man.get("frames", []):
            for k in ("hh", "hv"):
                if f.get(k) and not os.path.exists(os.path.join(DST, ds["dir"], f[k])):
                    missing.append(f"{ds['dir']}/{f[k]}")
    if missing:
        raise SystemExit("dist/ is incomplete, missing: " + ", ".join(missing[:10]))
    n = sum(len(fs) for _, _, fs in os.walk(DST))
    size = sum(os.path.getsize(os.path.join(r, x)) for r, _, fs in os.walk(DST) for x in fs)
    print(f"dist/: {n} files, {size / 1e6:.0f} MB, {len(menu)} datasets, "
          f"Google Maps key {'INCLUDED' if with_maps_key else 'not included'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--with-maps-key", action="store_true", help="include viewer/config.js (restricted key only)")
    build(ap.parse_args().with_maps_key)
