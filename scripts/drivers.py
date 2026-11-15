"""Possible drivers of each event: how unusual were the conditions before it, compared with the same window in past years?

    python scripts/drivers.py langtang            # one site
    python scripts/drivers.py all                 # every site
    python scripts/drivers.py langtang --offline  # recompute from the saved data only (no API calls)

Data: Meteomatics (historic model/reanalysis data, daily, 1980 onwards) and the USGS earthquake catalogue.
Everything downloaded is saved under meteo/ so results can be recomputed after the Meteomatics account expires.
Output: viewer/<dataset>/drivers.json, shown by the viewer as a "Possible drivers" card.

Method (per factor): value in a window before the event (e.g. rain in the 30 days before) and the same window in
every baseline year (1980-2025). Percentile = share of baseline years with a lower value. This says how UNUSUAL a
condition was, not that it caused the event: the summary names factors that were both unusual and physically able
to contribute to this kind of event.
"""
import argparse, csv, datetime as dt, json, math, os, sys, time
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(ROOT, "meteo")
API = "https://api.meteomatics.com"
BASE_START, BASE_END = 1980, 2025
RECENT_START = 2010          # verdicts use 2010-2025: allows for warming and for breaks in the older model record

# Data-quality notes from checking each series year by year (see README "Possible drivers").
CONFIDENCE = {
    "soil_moisture_index_-50cm:idx": ("low", "the model's deep soil moisture drops sharply after 2022 with normal rain; "
                                             "not used in the summary"),
    "soil_moisture_index_-15cm:idx": ("low", "model soil moisture; recent years drift lower; not used in the summary"),
    "freezing_level:m": ("medium", "the series steps up by ~150 m around 2014 while temperature does not; "
                                   "compare with the temperature factors"),
}

# ------------------------------------------------------------------------------------------------ sites
# Each factor: key, label, point, daily series, aggregation over the window, window length (days), physical role.
# role = how the factor could contribute ("high" = high values are the risky side, "low" = low values are).
SITES = {
    "langtang": {
        "dataset": "data_nepal", "also": ["data_langtang"],
        "title": "Langtang Lirung ice-rock avalanche and Bhote Koshi flood",
        "event": "2026-08-26", "event_utc": "2026-08-26T02:52:10Z",
        "kind": "ice-rock avalanche",
        "points": {
            "source": {"lat": 28.285, "lon": 85.505, "name": "Langtang Lirung north flank (source, approx., ~5,150 m)"},
            "valley": {"lat": 28.161, "lon": 85.344, "name": "Syabrubesi (valley)"},
        },
        "source_elev_m": 5150,
        "factors": [
            ("t500_30", "Air temperature at ~5.5 km, 30-day mean", "source", "t_500hPa:C", "mean", 30, "high", "°C",
             "warm air at glacier height melts ice and thaws frozen rock"),
            ("t500_60", "Air temperature at ~5.5 km, 60-day mean", "source", "t_500hPa:C", "mean", 60, "high", "°C",
             "a long warm spell lets heat soak into the slope"),
            ("fzl_30", "Freezing level, 30-day mean", "source", "freezing_level:m", "mean", 30, "high", "m",
             "the higher the 0 °C line, the more of the slope is above freezing"),
            ("fzl_60", "Freezing level, 60-day mean", "source", "freezing_level:m", "mean", 60, "high", "m",
             "a long spell with the 0 °C line high above the source"),
            ("tmax_30", "Daily maximum temperature, 30-day mean", "source", "t_max_2m_24h:C", "mean", 30, "high", "°C",
             "surface melt"),
            ("melt_30", "Snowmelt, 30-day total", "source", "snow_melt_24h:mm", "sum", 30, "high", "mm",
             "meltwater entering cracks raises water pressure"),
            ("snow_30", "Snow depth, 30-day mean", "source", "snow_depth:cm", "mean", 30, "low", "cm",
             "little snow leaves rock and ice exposed to heat"),
            ("rain_3", "Rain, 3 days before", "source", "precip_24h:mm", "sum", 3, "high", "mm", "rain trigger"),
            ("rain_30", "Rain, 30 days before", "source", "precip_24h:mm", "sum", 30, "high", "mm",
             "monsoon water loading"),
            ("soil_7", "Soil moisture (50 cm), 7-day mean", "valley", "soil_moisture_index_-50cm:idx", "mean", 7, "high",
             "index", "wet ground downstream erodes and fails more easily"),
        ],
        "quake": {"radius_km": 300, "days": 30, "min_mag": 4.0},
    },
    "budhi": {
        "dataset": "data_gorkha",
        "title": "Budhi Gandaki landslide-dam flood near Chumchet",
        "event": "2026-10-11", "event_utc": "2026-10-10T22:37:00Z",
        "kind": "landslide-dam breach flood",
        "points": {
            "source": {"lat": 28.4755, "lon": 84.9644, "name": "Chumchet (landslide area)"},
            "valley": {"lat": 28.045, "lon": 84.815, "name": "Arughat (valley)"},
        },
        "source_elev_m": 2500,
        "factors": [
            ("rain_1", "Rain, day before", "source", "precip_24h:mm", "sum", 1, "high", "mm", "rain trigger"),
            ("rain_3", "Rain, 3 days before", "source", "precip_24h:mm", "sum", 3, "high", "mm", "rain trigger"),
            ("rain_30", "Rain, 30 days before", "source", "precip_24h:mm", "sum", 30, "high", "mm",
             "late-monsoon saturation"),
            ("rain_90", "Rain, 90 days before (monsoon)", "source", "precip_24h:mm", "sum", 90, "high", "mm",
             "slopes soaked by the whole monsoon"),
            ("soil_15_7", "Soil moisture (15 cm), 7-day mean", "source", "soil_moisture_index_-15cm:idx", "mean", 7,
             "high", "index", "saturated surface layer"),
            ("soil_50_30", "Soil moisture (50 cm), 30-day mean", "source", "soil_moisture_index_-50cm:idx", "mean", 30,
             "high", "index", "deep saturation weakens slopes"),
            ("fzl_30", "Freezing level, 30-day mean", "source", "freezing_level:m", "mean", 30, "high", "m",
             "melt in the headwaters"),
            ("t500_30", "Air temperature at ~5.5 km, 30-day mean", "source", "t_500hPa:C", "mean", 30, "high", "°C",
             "heat in the headwaters"),
        ],
        "quake": {"radius_km": 300, "days": 30, "min_mag": 4.0},
    },
    "padma": {
        "dataset": "data_bangladesh",
        "title": "Padma River monsoon dynamics at Sadarpur",
        "event": "2026-10-18", "event_utc": "2026-10-18T12:34:00Z",
        "kind": "monsoon river change / erosion",
        "points": {
            "local": {"lat": 23.48, "lon": 90.03, "name": "Sadarpur, Faridpur"},
            "ganges": {"multi": [(30.0, 79.0), (27.5, 81.5), (26.5, 84.0), (27.7, 85.3), (25.6, 85.1), (24.8, 87.9)],
                       "name": "Ganges basin (6-point mean)"},
            "brahma": {"multi": [(29.3, 91.1), (27.5, 94.9), (26.2, 91.7), (25.6, 89.7), (27.4, 88.6)],
                       "name": "Brahmaputra basin (5-point mean)"},
        },
        "factors": [
            ("ganges_season", "Ganges basin rain, 1 Jun to 18 Oct", "ganges", "precip_24h:mm", "sum", 140, "high", "mm",
             "more upstream rain, more water and sediment in the Padma"),
            ("brahma_season", "Brahmaputra basin rain, 1 Jun to 18 Oct", "brahma", "precip_24h:mm", "sum", 140, "high",
             "mm", "the Jamuna (Brahmaputra) joins the Padma just upstream"),
            ("ganges_peak30", "Ganges basin rain, wettest 30 days of the season", "ganges", "precip_24h:mm",
             "max_run_sum:30", 140, "high", "mm", "flood peak"),
            ("brahma_peak30", "Brahmaputra basin rain, wettest 30 days of the season", "brahma", "precip_24h:mm",
             "max_run_sum:30", 140, "high", "mm", "flood peak"),
            ("local_season", "Local rain at Sadarpur, 1 Jun to 18 Oct", "local", "precip_24h:mm", "sum", 140, "high",
             "mm", "local bank saturation"),
            ("recession", "Basin rain drop: last 30 days vs wettest 30 days", "brahma", "precip_24h:mm",
             "recession:30", 140, "high", "share",
             "banks often collapse while the water falls quickly after the peak"),
            ("wind_30", "Wind speed at Sadarpur, 30-day mean", "local", "wind_speed_10m:ms", "mean", 30, "high", "m/s",
             "waves on the river attack the banks"),
        ],
        "quake": None,
    },
    "volcano": {
        "dataset": "data",
        "title": "Krasheninnikov eruption",
        "event": "2025-08-02", "event_utc": "2025-08-02T16:50:00Z",
        "kind": "volcanic eruption",
        "points": {"source": {"lat": 54.593, "lon": 160.273, "name": "Krasheninnikov summit"}},
        "factors": [
            ("rain_30", "Rain, 30 days before", "source", "precip_24h:mm", "sum", 30, "high", "mm",
             "water reaching hot rock can cause steam explosions (minor trigger)"),
            ("melt_30", "Snowmelt, 30 days before", "source", "snow_melt_24h:mm", "sum", 30, "high", "mm",
             "meltwater into the volcano (minor trigger)"),
            ("t2m_30", "Air temperature, 30-day mean", "source", "t_2m:C", "mean", 30, "high", "°C", "control"),
            ("msl_drop_3", "Largest 3-day drop in air pressure, last 30 days", "source", "msl_pressure:hPa",
             "max_drop:3", 30, "high", "hPa", "pressure changes have been linked to small eruptions (debated)"),
        ],
        "quake": {"radius_km": 400, "days": 30, "min_mag": 5.0},
    },
}

# Daily series to fetch: accumulations at 00Z (they cover the previous 24 h -> assigned to the previous day);
# states at 06Z (~noon in Nepal / Bangladesh) or 00Z for Kamchatka.
ACCUM = {"precip_24h:mm", "snow_melt_24h:mm", "t_max_2m_24h:C"}


# ------------------------------------------------------------------------------------------------ Meteomatics
def creds():
    """(user, password): environment variables, then the root .env, then scripts/meteomatics.env."""
    from settings import meteomatics_auth
    return meteomatics_auth()


def mm_query(auth, start, end, hour, params, coords):
    """Daily values at HH:00Z from start to end (dates) for several params and coordinates. Returns
    {param: {(lat,lon): {date: value}}}."""
    url = (f"{API}/{start}T{hour:02d}:00:00Z--{end}T{hour:02d}:00:00Z:P1D/{','.join(params)}/"
           f"{'+'.join(f'{a},{b}' for a, b in coords)}/json")
    for attempt in range(5):
        try:
            r = requests.get(url, auth=auth, timeout=180)
            if r.status_code == 200:
                break
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(5 * (attempt + 1)); continue
            raise RuntimeError(f"Meteomatics {r.status_code}: {r.text[:300]}")
        except requests.RequestException as e:
            if attempt == 4:
                raise
            time.sleep(5 * (attempt + 1))
    out = {}
    for blk in r.json()["data"]:
        per = out.setdefault(blk["parameter"], {})
        for c in blk["coordinates"]:
            ser = per.setdefault((round(c["lat"], 4), round(c["lon"], 4)), {})
            for d in c["dates"]:
                ser[d["date"][:10]] = d["value"]
    return out


def series_path(site, pkey):
    return os.path.join(CACHE, f"{site}_{pkey}.csv")


def load_cache(site, pkey):
    p = series_path(site, pkey)
    data = {}
    if os.path.exists(p):
        for row in csv.DictReader(open(p, newline="", encoding="utf-8")):
            d = row.pop("date")
            for k, v in row.items():
                if v != "":
                    data.setdefault(k, {})[d] = float(v)
    return data


def save_cache(site, pkey, data):
    os.makedirs(CACHE, exist_ok=True)
    params = sorted(data)
    dates = sorted({d for s in data.values() for d in s})
    with open(series_path(site, pkey), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date"] + params)
        for d in dates:
            w.writerow([d] + [("" if data[p].get(d) is None else data[p][d]) for p in params])


def fetch_site(site, cfg, auth):
    """Fetch every needed (point, param) series 1980-01-01 .. event, using and extending the local cache."""
    need = {}
    for f in cfg["factors"]:
        need.setdefault(f[2], set()).add(f[3])
    end = cfg["event"]
    utc_noon = 0 if cfg["points"].get("source", {}).get("lon", 0) > 140 else 6
    for pkey, params in need.items():
        pt = cfg["points"][pkey]
        coords = pt["multi"] if "multi" in pt else [(pt["lat"], pt["lon"])]
        data = load_cache(site, pkey)
        todo = [p for p in params if p not in data or max(data[p], default="") < (dt.date.fromisoformat(end) -
                                                                                 dt.timedelta(days=1)).isoformat()]
        if not todo:
            print(f"  {site}/{pkey}: cached"); continue
        acc = [p for p in todo if p in ACCUM]
        inst = [p for p in todo if p not in ACCUM]
        for group, hour, shift in ((acc, 0, -1), (inst, utc_noon, 0)):
            if not group:
                continue
            for y0 in range(BASE_START, int(end[:4]) + 1, 8):          # 8-year chunks keep each request small
                a = f"{y0}-01-01"
                b = min(dt.date(y0 + 7, 12, 31), dt.date.fromisoformat(end) + dt.timedelta(days=1)).isoformat()
                if a > b:
                    continue
                print(f"  {site}/{pkey}: {','.join(group)} {a}..{b}")
                res = mm_query(auth, a, b, hour, group, coords)
                for prm, per in res.items():
                    # multi-point: mean over the points; accumulations: shift to the day they cover
                    days = sorted({d for s in per.values() for d in s})
                    for d in days:
                        vals = [s[d] for s in per.values() if s.get(d) is not None and s[d] > -900]
                        if not vals:
                            continue
                        day = (dt.date.fromisoformat(d) + dt.timedelta(days=shift)).isoformat()
                        data.setdefault(prm, {})[day] = sum(vals) / len(vals)
                time.sleep(0.4)
        save_cache(site, pkey, data)


# ------------------------------------------------------------------------------------------------ analysis
def window_dates(end_date, ndays):
    """The ndays days before end_date (event day excluded)."""
    return [(end_date - dt.timedelta(days=i)).isoformat() for i in range(ndays, 0, -1)]


def aggregate(series, dates, how):
    vals = [series.get(d) for d in dates]
    if sum(v is not None for v in vals) < 0.8 * len(dates):
        return None
    v = [x for x in vals if x is not None]
    if how == "sum":
        return sum(v)
    if how == "mean":
        return sum(v) / len(v)
    if how.startswith("days_above:"):
        thr = float(how.split(":")[1]); return float(sum(x > thr for x in v))
    if how.startswith("max_run_sum:"):
        n = int(how.split(":")[1]); return max(sum(v[i:i + n]) for i in range(0, len(v) - n + 1))
    if how.startswith("recession:"):
        n = int(how.split(":")[1]); peak = max(sum(v[i:i + n]) for i in range(0, len(v) - n + 1))
        return 1 - sum(v[-n:]) / peak if peak > 0 else None
    if how.startswith("max_drop:"):
        n = int(how.split(":")[1]); return max(v[i] - v[i + n] for i in range(0, len(v) - n))
    raise ValueError(how)


def analyse(site, cfg):
    ev = dt.date.fromisoformat(cfg["event"])
    caches = {pk: load_cache(site, pk) for pk in cfg["points"]}
    rows = []
    for key, label, pkey, prm, how, nd, role, unit, why in cfg["factors"]:
        ser = caches.get(pkey, {}).get(prm, {})
        if site == "padma":                          # season window: 1 Jun .. event day
            nd = (ev - dt.date(ev.year, 6, 1)).days
        val = aggregate(ser, window_dates(ev, nd), how)
        base, base_recent = [], []
        for y in range(BASE_START, BASE_END + 1):
            if y == ev.year:
                continue
            try:
                e2 = ev.replace(year=y)
            except ValueError:
                e2 = ev.replace(year=y, day=28)
            b = aggregate(ser, window_dates(e2, nd), how)
            if b is not None:
                base.append(b)
                if y >= RECENT_START:
                    base_recent.append(b)
        if val is None or len(base) < 10:
            rows.append(dict(key=key, label=label, status="no data")); continue
        pctl = lambda bs: 100 * (sum(b < val for b in bs) + 0.5 * sum(b == val for b in bs)) / len(bs)
        pct, pct_recent = pctl(base), pctl(base_recent)
        mean = sum(base) / len(base)
        sd = math.sqrt(sum((b - mean) ** 2 for b in base) / max(1, len(base) - 1))
        z = (val - mean) / sd if sd > 0 else 0.0
        risky_all = pct if role == "high" else 100 - pct           # percentile on the risky side
        risky = pct_recent if role == "high" else 100 - pct_recent
        # exceptional: top 5% of 1980-2025 and top 10% of 2010-2025; unusual: top 20% in both baselines
        if risky_all >= 95 and risky >= 90:
            verdict = "exceptional"
        elif min(risky_all, risky) >= 80:
            verdict = "unusual"
        elif max(risky_all, risky) <= 10:
            verdict = "unusually " + ("low" if role == "high" else "high")
        else:
            verdict = "normal"
        conf, conf_note = CONFIDENCE.get(prm, ("good", ""))
        rank = 1 + sum(b > val for b in base) if role == "high" else 1 + sum(b < val for b in base)
        rows.append(dict(key=key, label=label, point=cfg["points"][pkey]["name"], value=round(val, 3),
                         unit=unit, normal=round(mean, 3), sd=round(sd, 3), percentile=round(pct, 1),
                         percentile_recent=round(pct_recent, 1), z=round(z, 2), risky_side=role,
                         risky_percentile=round(risky, 1), risky_percentile_all=round(risky_all, 1), verdict=verdict,
                         rank=f"{rank} of {len(base) + 1}", years=f"{BASE_START}-{BASE_END}",
                         recent_years=f"{RECENT_START}-{BASE_END}", window_days=nd, why=why,
                         confidence=conf, confidence_note=conf_note))
    return rows


# ------------------------------------------------------------------------------------------------ earthquakes
def haversine(a, b):
    R = 6371.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def quakes(site, cfg, offline):
    q = cfg.get("quake")
    if not q:
        return None
    src = cfg["points"]["source"]
    path = os.path.join(CACHE, f"{site}_quakes.json")
    ev_t = dt.datetime.fromisoformat(cfg["event_utc"].replace("Z", "+00:00"))
    if not (offline and os.path.exists(path)):
        r = requests.get("https://earthquake.usgs.gov/fdsnws/event/1/query", timeout=60, params=dict(
            format="geojson", latitude=src["lat"], longitude=src["lon"], maxradiuskm=q["radius_km"],
            minmagnitude=q["min_mag"], starttime=(ev_t - dt.timedelta(days=q["days"])).isoformat(),
            endtime=ev_t.isoformat(), orderby="magnitude"))
        r.raise_for_status()
        os.makedirs(CACHE, exist_ok=True)
        json.dump(r.json(), open(path, "w"))
    feats = json.load(open(path))["features"]
    ev_list = []
    for f in feats:
        lon, lat = f["geometry"]["coordinates"][:2]
        t = dt.datetime.fromtimestamp(f["properties"]["time"] / 1000, dt.timezone.utc)
        if t >= ev_t:          # the event's own seismic signal is not a cause
            continue
        ev_list.append(dict(mag=f["properties"]["mag"], place=f["properties"]["place"], km=round(haversine(
            (src["lat"], src["lon"]), (lat, lon))), days_before=round((ev_t - t).total_seconds() / 86400, 1),
            time=t.isoformat()))
    ev_list.sort(key=lambda e: -e["mag"])
    biggest = ev_list[0] if ev_list else None
    # a rough "shaking reach" rule: big quakes can disturb volcanoes / slopes out to ~10^(0.5 M - 1.8) x 100 km
    reach = (lambda m: 10 ** (0.5 * m - 1.8) * 100)
    within = [e for e in ev_list if e["km"] <= reach(e["mag"])]
    verdict = "exceptional" if any(e["mag"] >= 7 for e in within) else "unusual" if within else "normal"
    return dict(radius_km=q["radius_km"], days=q["days"], min_mag=q["min_mag"], count=len(ev_list),
                biggest=biggest, close_enough=len(within), verdict=verdict,
                note="earthquakes before the event within the search radius (USGS); 'close enough' uses a rough "
                     "magnitude-distance reach for dynamic triggering")


# ------------------------------------------------------------------------------------------------ summary
def topic(label):
    """'Rain, 30 days before' -> 'rain'; keeps capitals of names such as Ganges."""
    t = label.split(", ")[0]
    return t[0].lower() + t[1:] if t[:2] != t[:2].upper() and t.split()[0] not in ("Ganges", "Brahmaputra", "Local") else t


def ordinal(rank):
    n = int(rank.split(" of ")[0])
    return "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def summarise(site, cfg, rows, eq):
    ok = [r for r in rows if r.get("verdict") and r.get("confidence") != "low"]
    risky = [r for r in ok if r["verdict"] in ("exceptional", "unusual")]
    risky.sort(key=lambda r: -r["risky_percentile"])
    parts, seen = [], set()
    if eq and eq["verdict"] != "normal" and eq["biggest"]:
        b = eq["biggest"]
        parts.append(f"a magnitude {b['mag']:.1f} earthquake {b['km']} km away, {b['days_before']:.0f} days before")
    for r in risky:
        t = topic(r["label"])
        if t in seen:
            continue
        seen.add(t)
        lab = r["label"][0].lower() + r["label"][1:] if r["label"].split()[0] not in ("Ganges", "Brahmaputra") else r["label"]
        parts.append(f"{lab} ({r['verdict']}: {r['rank'].split(' of ')[0]}{ordinal(r['rank'])} highest of "
                     f"{r['rank'].split(' of ')[1]} years{', medium confidence' if r['confidence'] == 'medium' else ''})"
                     if r["risky_side"] == "high" else f"{lab} ({r['verdict']})")
    low, normal = [], []
    for r in ok:
        t = topic(r["label"])
        if r["verdict"].startswith("unusually") and t not in seen and t not in low:
            low.append(t); seen.add(t)
    for r in ok:
        t = topic(r["label"])
        if r["verdict"] == "normal" and t not in seen and t not in normal:
            normal.append(t)
    if eq and eq["verdict"] == "normal":
        normal.append("earthquakes")
    if parts:
        lead = "Stood out before the event: " + "; ".join(parts) + "."
    else:
        lead = "No factor stood out: conditions were within the normal range for this time of year."
    if low:
        lead += " Unusually low (the other way): " + ", ".join(low) + "."
    if normal:
        lead += " Within the normal range: " + ", ".join(normal) + "."
    lead += " Unusual is not the same as causal: these are candidates, not proof."
    return lead


def run(site, offline):
    cfg = SITES[site]
    print(f"== {site}: {cfg['title']} ({cfg['event']})")
    if not offline:
        fetch_site(site, cfg, creds())
    rows = analyse(site, cfg)
    eq = quakes(site, cfg, offline)
    out = dict(site=site, title=cfg["title"], kind=cfg["kind"], event=cfg["event"], event_utc=cfg["event_utc"],
               points={k: {kk: vv for kk, vv in v.items()} for k, v in cfg["points"].items()},
               baseline=f"{BASE_START}-{BASE_END}", factors=rows, earthquakes=eq, summary=summarise(site, cfg, rows, eq),
               sources=["Meteomatics Weather API (historic model and reanalysis data)", "USGS earthquake catalogue"],
               generated=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    for ds in [cfg["dataset"]] + cfg.get("also", []):
        dest = os.path.join(ROOT, "viewer", ds, "drivers.json")
        json.dump(out, open(dest, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    for r in rows:
        if r.get("verdict"):
            print(f"  {r['label'][:52]:52s} {r['value']:>9.2f} {r['unit']:5s} normal {r['normal']:>9.2f}  "
                  f"pct {r['percentile']:5.1f} (2010+: {r['percentile_recent']:5.1f})  z {r['z']:+5.2f}  "
                  f"{r['verdict']}{'' if r['confidence'] == 'good' else '  [' + r['confidence'] + ' confidence]'}")
        else:
            print(f"  {r['label'][:58]:58s} {r['status']}")
    if eq:
        b = eq["biggest"]
        print(f"  earthquakes: {eq['count']} M>={eq['min_mag']} within {eq['radius_km']} km in {eq['days']} d; "
              f"biggest {('M%.1f %s, %d km, %.1f d before' % (b['mag'], b['place'], b['km'], b['days_before'])) if b else 'none'}"
              f" -> {eq['verdict']}")
    print("  SUMMARY:", out["summary"])
    print("  ->", dest)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("site", choices=list(SITES) + ["all"])
    ap.add_argument("--offline", action="store_true", help="use saved data only")
    a = ap.parse_args()
    for s in (SITES if a.site == "all" else [a.site]):
        run(s, a.offline)
