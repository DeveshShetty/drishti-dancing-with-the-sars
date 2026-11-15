"""Plain-language explanation and short-term outlook for one dataset, written by Gemini.

    python scripts/explain.py data_langtang          # one dataset (folder name under viewer/)
    python scripts/explain.py all                    # every dataset in viewer/datasets.json
    python scripts/explain.py data_gorkha --dry-run  # gather + build the prompt, no API call

Inputs, in one multimodal request:
  1. time-lapse frames: a few radar passes of the dataset (downscaled JPEGs), always incl. the passes around the event
  2. EDA results: viewer/<dataset>/drivers.json (scripts/drivers.py) + per-pass radar statistics computed here
  3. stored history: meteo/<site>_*.csv (daily weather 1980 onwards), meteo/<site>_quakes.json, the dataset manifest
Output: viewer/<dataset>/explain.json, shown by the viewer's "Explanation" card. If Gemini is unavailable (no key,
API error, rate limit) a rule-based fallback is written instead, marked as such, so the viewer never breaks.

Config (.env at the project root, environment variables, or scripts/gemini.env): GEMINI_API_KEY, GEMINI_MODEL.
"""
import argparse, base64, csv, datetime as dt, io, json, math, os, sys, time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
VIEWER = os.path.join(ROOT, "viewer")
METEO = os.path.join(ROOT, "meteo")
sys.path.insert(0, HERE)

MAX_FRAMES = 6            # time-lapse frames sent to the model
FRAME_PX = 512            # longest side of each frame
STATS_STEP = 4            # per-pass statistics on every 4th pixel (enough for scene-level numbers, keeps memory low)
TREND_YEARS = 20          # yearly history shown for the top weather factors
DEFAULT_MODEL = "gemini-3.8-flash"


# ------------------------------------------------------------------------------------------------ config
def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_config(path=None):
    """GEMINI_API_KEY / GEMINI_MODEL. Default lookup: environment variables, then the root .env, then
    scripts/gemini.env (see settings.py). With an explicit path, only that file and the environment are read."""
    from settings import get_settings, read_env_file
    names = ["GEMINI_API_KEY", "GEMINI_MODEL"]
    if path is None:
        cfg = get_settings(names, legacy=os.path.join(HERE, "gemini.env"))
    else:
        cfg = {k: v for k, v in read_env_file(path).items() if v}
        cfg.update({k: os.environ[k] for k in names if os.environ.get(k)})
    cfg.setdefault("GEMINI_MODEL", DEFAULT_MODEL)
    return cfg


# ------------------------------------------------------------------------------------------------ gathering
def decode_db(img_u8, db_lo=-30.0, db_hi=5.0):
    """Viewer PNG byte -> dB (byte 0 = no data)."""
    a = img_u8.astype("float32")
    return np.where(a > 0, db_lo + (a - 1) / 254 * (db_hi - db_lo), np.nan)


def pick_sensor(frames):
    """Sensor with the most passes (the longest history); ties go to the first listed."""
    counts = {}
    for f in frames:
        counts[f.get("sensor") or "Radar"] = counts.get(f.get("sensor") or "Radar", 0) + 1
    return max(counts, key=lambda s: counts[s]) if counts else None


def sample_frames(frames, event, n=MAX_FRAMES):
    """Up to n passes in date order: the last pass before and first after the event are always included, the rest
    spread evenly over the record. Averaged passes (label set) are left out: they are not real acquisitions."""
    fr = sorted([f for f in frames if not f.get("label")], key=lambda f: f["date"])
    if len(fr) <= n:
        return fr
    keep = set()
    if event:
        before = [i for i, f in enumerate(fr) if f["date"] < event]
        after = [i for i, f in enumerate(fr) if f["date"] >= event]
        if before:
            keep.add(before[-1])
        if after:
            keep.add(after[0])
    keep.update({0, len(fr) - 1})
    for x in np.linspace(0, len(fr) - 1, n):
        if len(keep) >= n:
            break
        keep.add(int(round(x)))
    return [fr[i] for i in sorted(keep)][:n]


def frame_jpeg(path, max_px=FRAME_PX):
    """Downscaled, contrast-stretched (2-98 %) greyscale JPEG of one radar pass, or None if unreadable."""
    try:
        with Image.open(path) as src:
            a = np.array(src).astype("float32")
    except (OSError, ValueError):
        return None
    v = a[a > 0]
    if v.size == 0:
        return None
    lo, hi = np.percentile(v, [2, 98])
    out = np.clip((a - lo) / max(hi - lo, 1) * 255, 0, 255).astype("uint8")
    out[a == 0] = 0
    im = Image.fromarray(out, "L")
    im.thumbnail((max_px, max_px))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=80)
    return b.getvalue()


def pass_statistics(dsdir, frames, meta):
    """Scene-level numbers for every pass of one sensor: median backscatter and how much of the scene changed by more
    than 3 dB since the previous pass and since the first pass. Cheap: every STATS_STEP-th pixel."""
    lo, hi = meta.get("db_range", [-30, 5])
    px_km2 = (meta.get("pixel_size_m", 30) * STATS_STEP) ** 2 / 1e6
    rows, first, prev = [], None, None
    for f in sorted(frames, key=lambda f: f["date"]):
        p = os.path.join(dsdir, f["hh"])
        if not os.path.exists(p):
            rows.append({"date": f["date"], "note": "image missing"}); continue
        with Image.open(p) as src:
            db = decode_db(np.array(src)[::STATS_STEP, ::STATS_STEP], lo, hi)
        r = {"date": f["date"], "label": f.get("label") or "", "median_db": round(float(np.nanmedian(db)), 2),
             "valid_pct": round(float(np.isfinite(db).mean() * 100), 1)}
        for name, ref in (("vs_previous", prev), ("vs_first", first)):
            if ref is not None:
                d = db - ref
                ok = np.isfinite(d)
                d = d - np.nanmedian(d)                     # remove overall brightness offset, as the viewer does
                r[f"brighter_km2_{name}"] = round(float(((d > 3) & ok).sum() * px_km2), 1)
                r[f"darker_km2_{name}"] = round(float(((d < -3) & ok).sum() * px_km2), 1)
        rows.append(r)
        if not f.get("label"):
            prev = db
            if first is None:
                first = db
    return rows


def weather_history(drivers, site):
    """Yearly values (last TREND_YEARS years) and linear trend per decade for the factors that stood out, plus the
    two most extreme of the rest, recomputed from the saved daily data with drivers.py's own definitions."""
    if not drivers or not site:
        return []
    try:
        import drivers as drv
    except ImportError:
        return []
    cfg = drv.SITES.get(site)
    if not cfg:
        return []
    ranked = sorted([f for f in drivers.get("factors", []) if f.get("verdict") and f.get("confidence") != "low"],
                    key=lambda f: -f.get("risky_percentile", 0))[:4]
    keys = {f["key"] for f in ranked}
    ev = dt.date.fromisoformat(cfg["event"])
    out = []
    for key, label, pkey, prm, how, nd, role, unit, why in cfg["factors"]:
        if key not in keys:
            continue
        ser = drv.load_cache(site, pkey).get(prm, {})
        if not ser:
            continue
        if site == "padma":
            nd = (ev - dt.date(ev.year, 6, 1)).days
        years = {}
        for y in range(ev.year - TREND_YEARS, ev.year + 1):
            try:
                e2 = ev.replace(year=y)
            except ValueError:
                e2 = ev.replace(year=y, day=28)
            v = drv.aggregate(ser, drv.window_dates(e2, nd), how)
            if v is not None:
                years[y] = round(v, 2)
        if len(years) >= 5:
            ys, vs = np.array(list(years)), np.array(list(years.values()))
            slope = float(np.polyfit(ys, vs, 1)[0]) * 10
        else:
            slope = None
        out.append({"factor": label, "unit": unit, "values_by_year": years,
                    "trend_per_decade": None if slope is None else round(slope, 3)})
    return out


def quake_summary(site):
    p = os.path.join(METEO, f"{site}_quakes.json")
    if not site or not os.path.exists(p):
        return None
    try:
        feats = read_json(p).get("features", [])
    except (OSError, ValueError):
        return None
    mags = sorted((f["properties"].get("mag") or 0 for f in feats), reverse=True)
    return {"count": len(feats), "largest_magnitudes": mags[:5]}


def gather(dataset, viewer_dir=VIEWER):
    """Everything sent to the model, plus a list of what was missing. Never raises for missing pieces."""
    dsdir = os.path.join(viewer_dir, dataset)
    problems = []
    mpath = os.path.join(dsdir, "manifest.json")
    if not os.path.exists(mpath):
        raise FileNotFoundError(f"{mpath} not found: is '{dataset}' a folder under viewer/?")
    meta = read_json(mpath)
    drivers = None
    dpath = os.path.join(dsdir, "drivers.json")
    if os.path.exists(dpath):
        try:
            drivers = read_json(dpath)
        except ValueError:
            problems.append("drivers.json is not valid JSON")
    else:
        problems.append("no EDA results (drivers.json): run scripts/drivers.py first")
    site = drivers.get("site") if drivers else None
    event = (meta.get("event") or {}).get("date") or (drivers or {}).get("event")
    sensor = pick_sensor(meta.get("frames", []))
    sframes = [f for f in meta.get("frames", []) if (f.get("sensor") or "Radar") == sensor]
    if not sframes:
        problems.append("the dataset has no radar passes")
    chosen = sample_frames(sframes, event)
    images = []
    for f in chosen:
        jpg = frame_jpeg(os.path.join(dsdir, f["hh"]))
        if jpg is None:
            problems.append(f"image for {f['date']} missing or unreadable")
            continue
        images.append({"date": f["date"], "polarisation": (f.get("pols") or ["?"])[0], "jpeg": jpg})
    stats = pass_statistics(dsdir, sframes, meta) if sframes else []
    history = weather_history(drivers, site)
    if site and not history:
        problems.append(f"no saved weather history for site '{site}' in meteo/")
    return {
        "dataset": dataset, "title": meta.get("title", dataset), "subtitle": meta.get("subtitle", ""),
        "event": event, "event_label": (meta.get("event") or {}).get("label", ""),
        "default_pair": meta.get("default_pair"),
        "sensor": sensor, "sensors": meta.get("sensors", []), "pixel_size_m": meta.get("pixel_size_m"),
        "bbox": meta.get("bbox"), "n_passes": len(sframes),
        "first_pass": min((f["date"] for f in sframes), default=None),
        "last_pass": max((f["date"] for f in sframes), default=None),
        "images": images, "pass_stats": stats, "drivers": drivers, "weather_history": history,
        "quakes": quake_summary(site), "site": site, "problems": problems,
    }


# ------------------------------------------------------------------------------------------------ prompt
def compact_drivers(drivers):
    """The EDA result without the bulky or internal fields."""
    if not drivers:
        return None
    keep = ("label", "value", "unit", "normal", "percentile", "percentile_recent", "risky_side", "verdict", "rank",
            "window_days", "why", "confidence", "confidence_note")
    return {"event": drivers.get("event"), "kind": drivers.get("kind"), "baseline": drivers.get("baseline"),
            "summary": drivers.get("summary"),
            "factors": [{k: f[k] for k in keep if k in f} for f in drivers.get("factors", []) if f.get("verdict")],
            "earthquakes": drivers.get("earthquakes")}


INSTRUCTIONS = """You are explaining satellite radar monitoring of a natural hazard site to a non-expert audience
(students, local officials, journalists). Use plain, everyday language. Avoid jargon; if you must use a technical
word (for example "backscatter" or "decibel"), explain it in a few words. Be honest about uncertainty.

How to read the images: each image is one radar pass over the same area, shown in date order, captioned with its
date. Brighter means the ground sends more radar energy back (rough ground, rock, debris, buildings, steep slopes
facing the satellite). Darker means smoother surfaces (calm water, wet snow, smooth mud) or slopes facing away.
Radar sees through cloud and at night. Black areas are no data. The radar does not see the event itself, only the
ground before and after it; passes are days apart, so short-lived things such as a flood peak are usually missed.

The weather and earthquake analysis shows which conditions were UNUSUAL before the event. Unusual is not proof of
cause: describe such factors as "likely contributors" or "candidates", never as the confirmed cause.

Confidence rules for the prediction: "high" only if the data shows the same pattern repeating over many past years
AND the radar record supports it; "medium" if one clear line of evidence supports it; "low" if it rests on general
seasonal reasoning, a short radar record (about one year or less) or a single event. Most predictions from this data
should be "low" or "medium".

Use ONLY the images and the data below. Do not invent facts, place names, casualty figures or dates that are not in
the data. When you mention a change, say which dates it appears between. When you use a number, take it from the data.

Your two tasks:
1. EXPLAIN what is happening at this site: what changed across the time-lapse (cite visible changes and the dates),
   and what the data patterns show (the weather and earthquake analysis, the per-pass change statistics).
2. PREDICT what is likely to happen next at this site over the coming weeks to months, based ONLY on the past trend
   in this data (seasonal patterns in the yearly history, long-term trends, whether the radar changes have persisted
   or faded). Give a confidence level (low, medium or high) and the main reasons. If the evidence is too thin to make
   a meaningful prediction, set "evidence_sufficient" to false and say plainly why, instead of guessing.

Reply with JSON only, matching this structure:
{
  "headline": "one sentence, plain language",
  "what_is_happening": "2 to 4 short paragraphs",
  "visible_changes": [{"between": "date - date", "change": "what you see"}],
  "key_patterns": ["short bullet", "..."],
  "prediction": {
    "evidence_sufficient": true,
    "outlook": "what is likely next, plain language",
    "time_horizon": "for example the next 1-3 months",
    "confidence": "low | medium | high",
    "reasons": ["reason grounded in the data", "..."],
    "what_would_change_this": "what new observation would change the outlook"
  },
  "caveats": ["short limitation", "..."]
}"""


def build_prompt(ctx):
    """Text part of the request: instructions + a compact JSON summary of the data (images go as separate parts)."""
    data = {
        "site": {"title": ctx["title"], "subtitle": ctx["subtitle"], "event_date": ctx["event"],
                 "event": ctx["event_label"], "bounding_box_lon_lat": ctx["bbox"]},
        "time_lapse": {"sensor": ctx["sensor"], "all_sensors": ctx["sensors"], "pixel_size_m": ctx["pixel_size_m"],
                       "passes_total": ctx["n_passes"], "first_pass": ctx["first_pass"], "last_pass": ctx["last_pass"],
                       "images_attached_in_order": [i["date"] for i in ctx["images"]]},
        "per_pass_radar_statistics": {
            "note": "area of the scene that got more than 3 dB brighter or darker (3 dB = twice or half the signal), "
                    "against the previous pass and against the first pass; overall brightness offset removed",
            "passes": ctx["pass_stats"]},
        "eda_possible_drivers": compact_drivers(ctx["drivers"]),
        "weather_history_by_year": ctx["weather_history"],
        "earthquake_catalogue_summary": ctx["quakes"],
        "data_gaps": ctx["problems"],
    }
    return INSTRUCTIONS + "\n\nDATA (JSON):\n" + json.dumps(data, ensure_ascii=False, default=str)


def build_contents(ctx):
    """Request parts: captioned images in date order, then the text prompt."""
    from google.genai import types
    parts = []
    for i, im in enumerate(ctx["images"], 1):
        parts.append(f"Image {i}: radar pass of {im['date']} ({ctx['sensor']}, {im['polarisation']} polarisation)")
        parts.append(types.Part.from_bytes(data=im["jpeg"], mime_type="image/jpeg"))
    parts.append(build_prompt(ctx))
    return parts


# ------------------------------------------------------------------------------------------------ Gemini call
class GeminiError(Exception):
    pass


def call_gemini(contents, api_key, model, client=None, retries=3, timeout_s=1200):
    """One request with retries on rate limits / server errors. Returns the parsed JSON reply.
    The timeout is long on purpose: a full request (6 images + data) can take over 10 minutes on gemini-3.8-flash."""
    if client is None:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_s * 1000))
    config = {"response_mime_type": "application/json", "temperature": 0.3}
    last = None
    for attempt in range(1, retries + 1):
        try:
            resp = client.models.generate_content(model=model, contents=contents, config=config)
            text = (getattr(resp, "text", None) or "").strip()
            if not text:
                raise GeminiError("Gemini returned an empty reply (possibly blocked by safety filters)")
            try:
                return json.loads(text), getattr(resp, "usage_metadata", None)
            except ValueError:
                # tolerate a fenced or prefixed reply
                s, e = text.find("{"), text.rfind("}")
                if s >= 0 and e > s:
                    return json.loads(text[s:e + 1]), getattr(resp, "usage_metadata", None)
                raise GeminiError("Gemini's reply was not valid JSON")
        except GeminiError:
            raise
        except Exception as e:                     # SDK raises APIError / ClientError / ServerError / network errors
            code = getattr(e, "code", None) or getattr(e, "status_code", None)
            last = e
            if code in (429, 500, 502, 503, 504) or code is None:
                if attempt < retries:
                    time.sleep(min(60, 8 * attempt))
                    continue
            break
    code = getattr(last, "code", None)
    hint = {401: "the API key was rejected", 403: "the key has no access to this model",
            404: "the model name in GEMINI_MODEL is not available", 429: "rate limit or quota reached"}.get(code, "")
    raise GeminiError(f"Gemini request failed{f' ({code})' if code else ''}: {hint or str(last)[:200]}")


# ------------------------------------------------------------------------------------------------ fallback
def fallback_result(ctx, reason):
    """Rule-based text from the EDA summary and pass statistics, used when Gemini is unavailable."""
    d = ctx.get("drivers") or {}
    stats = [s for s in ctx.get("pass_stats", []) if "brighter_km2_vs_previous" in s]
    big = max(stats, key=lambda s: s["brighter_km2_vs_previous"] + s["darker_km2_vs_previous"], default=None)
    happening = [f"{ctx['title']}: {ctx['n_passes']} radar passes from {ctx['first_pass']} to {ctx['last_pass']}."]
    if big:
        happening.append(f"The largest change between two consecutive passes was up to {big['date']}: "
                         f"{big['brighter_km2_vs_previous']} km² got brighter and {big['darker_km2_vs_previous']} km² "
                         f"got darker by more than 3 dB.")
    if d.get("summary"):
        happening.append(d["summary"])
    return {
        "headline": f"Automatic summary for {ctx['title']} (AI explanation unavailable).",
        "what_is_happening": "\n\n".join(happening),
        "visible_changes": [], "key_patterns": [],
        "prediction": {"evidence_sufficient": False, "outlook": "No prediction: the AI explanation could not be "
                       "generated, and this fallback does not forecast.", "time_horizon": "", "confidence": "low",
                       "reasons": [reason], "what_would_change_this": ""},
        "caveats": ["Rule-based fallback text, not written by Gemini."],
    }


# ------------------------------------------------------------------------------------------------ run
def explain(dataset, cfg=None, client=None, viewer_dir=VIEWER, dry_run=False, write=True):
    cfg = cfg if cfg is not None else load_config()
    ctx = gather(dataset, viewer_dir)
    contents_text = build_prompt(ctx)
    if dry_run:
        return {"dry_run": True, "prompt_chars": len(contents_text), "images": [i["date"] for i in ctx["images"]],
                "image_bytes": sum(len(i["jpeg"]) for i in ctx["images"]), "problems": ctx["problems"]}
    source, error, usage = "gemini", None, None
    if not ctx["images"] and not ctx["drivers"] and not ctx["pass_stats"]:
        source, error = "fallback", "no images, EDA results or radar statistics to explain"
        result = fallback_result(ctx, error)
    elif not cfg.get("GEMINI_API_KEY") and client is None:
        source, error = "fallback", "GEMINI_API_KEY is not set (scripts/gemini.env or environment variable)"
        result = fallback_result(ctx, error)
    else:
        try:
            result, usage = call_gemini(build_contents(ctx), cfg.get("GEMINI_API_KEY"), cfg["GEMINI_MODEL"],
                                        client=client)
        except GeminiError as e:
            source, error = "fallback", str(e)
            result = fallback_result(ctx, error)
    out = {"dataset": dataset, "source": source, "model": cfg.get("GEMINI_MODEL") if source == "gemini" else None,
           "error": error, "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "inputs": {"images": [i["date"] for i in ctx["images"]], "sensor": ctx["sensor"],
                      "eda": bool(ctx["drivers"]), "weather_history_factors": len(ctx["weather_history"]),
                      "passes_in_statistics": len(ctx["pass_stats"]), "data_gaps": ctx["problems"]},
           "usage": None if usage is None else {"prompt_tokens": getattr(usage, "prompt_token_count", None),
                                                "output_tokens": getattr(usage, "candidates_token_count", None)},
           "result": result}
    if write:
        with open(os.path.join(viewer_dir, dataset, "explain.json"), "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1, ensure_ascii=False)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dataset", help="folder under viewer/ (e.g. data_langtang) or 'all'")
    ap.add_argument("--dry-run", action="store_true", help="gather and build the prompt only, no API call")
    a = ap.parse_args()
    if a.dataset == "all":
        names = [d["dir"] for d in read_json(os.path.join(VIEWER, "datasets.json"))]
    else:
        names = [a.dataset]
    cfg = load_config()
    for n in names:
        print(f"== {n}")
        try:
            out = explain(n, cfg, dry_run=a.dry_run)
        except FileNotFoundError as e:
            print("   error:", e); continue
        if a.dry_run:
            print(f"   prompt {out['prompt_chars']} chars, images {out['images']} ({out['image_bytes'] // 1024} KB)")
            for p in out["problems"]:
                print("   gap:", p)
            continue
        r = out["result"]
        print(f"   source: {out['source']}{'  (' + out['error'] + ')' if out['error'] else ''}")
        if out["usage"]:
            print(f"   tokens: {out['usage']['prompt_tokens']} in / {out['usage']['output_tokens']} out")
        print("   headline:", r.get("headline"))
        pr = r.get("prediction") or {}
        print(f"   prediction ({pr.get('confidence')}, evidence sufficient: {pr.get('evidence_sufficient')}):",
              (pr.get("outlook") or "")[:200])
        print("   ->", os.path.join(VIEWER, n, "explain.json"))


if __name__ == "__main__":
    main()
