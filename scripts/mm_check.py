"""Check what the Meteomatics account allows: limits, how far back history goes, which parameters work.
   python scripts/mm_check.py
Reads the login from .env (or environment variables, or scripts/meteomatics.env). Never prints it."""
import json, os, sys, time
import requests

HERE = os.path.dirname(os.path.abspath(__file__))


def creds():
    """(user, password): environment variables, then the root .env, then scripts/meteomatics.env."""
    from settings import meteomatics_auth
    return meteomatics_auth()


AUTH = creds()
API = "https://api.meteomatics.com"


def get(path, timeout=60):
    r = requests.get(f"{API}/{path}", auth=AUTH, timeout=timeout)
    return r.status_code, r.text


if __name__ == "__main__":
    code, txt = get("user_stats_json")
    print("user_stats:", code)
    if code == 200:
        try:
            st = json.loads(txt)
            print(json.dumps(st, indent=1)[:3000])
        except Exception:
            print(txt[:1500])
    else:
        print(txt[:500])

    # Langtang valley (Kyanjin Gompa) as the test point
    pt = "28.211,85.562"
    print("\nHistory depth (daily precipitation, 1 day):")
    for d in ["1980-08-01", "1990-08-01", "2000-08-01", "2010-08-01", "2020-08-01", "2025-08-01", "2026-08-25"]:
        code, txt = get(f"{d}T00:00:00Z/precip_24h:mm/{pt}/json")
        ok = "OK " if code == 200 else f"{code}"
        print(f"  {d}: {ok} {'' if code == 200 else txt[:140].replace(chr(10), ' ')}")
        time.sleep(0.3)

    print("\nParameters (at 2026-08-20, Langtang):")
    params = ["t_2m:C", "t_max_2m_24h:C", "t_min_2m_24h:C", "precip_24h:mm", "precip_1h:mm",
              "t_500hPa:C", "t_600hPa:C", "freezing_level:m", "frost_depth:cm",
              "snow_depth:cm", "fresh_snow_24h:cm", "snow_melt_24h:mm", "snow_water_eq:mm",
              "soil_moisture_index_-15cm:idx", "soil_moisture_index_-50cm:idx", "volumetric_soil_water_0cm_to_7cm:m3m3",
              "msl_pressure:hPa", "sfc_pressure:hPa", "wind_speed_10m:ms", "relative_humidity_2m:p",
              "evapotranspiration_24h:mm", "global_rad:W", "elevation:m", "is_rain_1h:idx"]
    good = []
    for prm in params:
        code, txt = get(f"2026-08-20T00:00:00Z/{prm}/{pt}/json")
        val = ""
        if code == 200:
            try:
                val = json.loads(txt)["data"][0]["coordinates"][0]["dates"][0]["value"]
            except Exception:
                val = "?"
            good.append(prm)
        print(f"  {prm:40s} {code} {val if code == 200 else txt[:110].replace(chr(10), ' ')}")
        time.sleep(0.3)
    print("\nworking:", ",".join(good))
