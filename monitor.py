"""
Daily spring N trigger monitor for all saved farm locations.

Reads locations.json, checks each location's 10cm soil temp trend and
rainfall forecast, and sends a push notification via ntfy.sh for any
location where conditions_met is true. Designed to run daily via
GitHub Actions at 9:30am NZ time.

Requires one environment variable: NTFY_TOPIC (your private ntfy.sh topic name)
"""

import json
import os
import smtplib
import sys
import time
from email.mime.text import MIMEText

import requests

SOIL_TEMP_THRESHOLD_C = 5.5      # growth-start threshold (Frame; ryegrass begins growth at this soil temp)
CONSECUTIVE_DAYS_REQUIRED = 5    # must hold at/above threshold for this many consecutive days
GDD_BASE_TEMP_C = 5.0            # base temp for growing degree day accumulation (NZ cool-season pasture convention)
GDD_START_MONTH_DAY = "06-01"    # accumulate GDD from 1 June each year (adjust to your local season start)
GDD_THRESHOLD_20KGDM = 150       # STARTING ESTIMATE ONLY — calibrate against your own farm-walk growth data
RAIN_FORECAST_DAYS = 5
RAIN_RISK_MM_5DAY = 40.0  # fallback flat threshold if soil moisture data is unavailable

# Root-zone soil moisture / leaching buffer assumptions.
# Ryegrass root zone is often quoted at 300-400mm; using 300mm (the
# conservative end) so the buffer calc doesn't overstate available capacity.
ROOT_ZONE_DEPTH_MM = 300
# Field capacity assumption for Canterbury's typical shallow stony soils —
# a rough default, not soil-specific. Adjust if you have better local data.
FIELD_CAPACITY_VWC = 0.30
# Small safety margin added on top of the calculated buffer, since the
# field capacity assumption above is approximate, not measured.
RAIN_SAFETY_MARGIN_MM = 5.0

# Rough presets for when exact S-map data isn't on hand. Field capacity
# (VWC) and root zone (mm) — deliberately approximate, meant as a
# better-than-default starting point, not a substitute for an actual
# S-map lookup. Reference these in locations.json with "soil_type": "light"
# etc., or override individually with "field_capacity_vwc"/"root_zone_depth_mm".
SOIL_TYPE_PRESETS = {
    "light": {"field_capacity_vwc": 0.20, "root_zone_depth_mm": 300},   # sandy/stony
    "medium": {"field_capacity_vwc": 0.28, "root_zone_depth_mm": 350},
    "heavy": {"field_capacity_vwc": 0.38, "root_zone_depth_mm": 400},   # silt/clay
}

# S-map lookup via Environment Canterbury's public ArcGIS service. Provides
# AWmm30 — available water capacity (mm) over a 300mm depth, i.e. the SIZE
# of the soil's water "tank" — not how full it currently is (that comes
# from Open-Meteo separately). Converting AW to an equivalent field capacity
# % needs a wilting point assumption, since AW = field capacity - wilting
# point; this uses a generic 10% VWC wilting point assumption (reasonable
# for most NZ topsoils, but a genuine approximation, not measured).
SMAP_SERVICE_URL = "https://gis.ecan.govt.nz/arcgis/rest/services/Public/Landcare_SMap_Layers/MapServer/5/query"
ASSUMED_WILTING_POINT_VWC = 0.10


def lookup_smap_field_capacity(lat, lon):
    """
    Queries ECan's public S-map service for the given point and returns a
    suggested field_capacity_vwc (and the fixed 300mm root zone it's based
    on), or None if there's no coverage at that point or the lookup fails.
    Mirrors the same logic used in the webpage's S-map lookup feature.
    """
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "AWmm30,SiblingTexture_Desc,SiblingDrainageCode_Desc,LongSoilName",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        resp = get_with_retry(SMAP_SERVICE_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None

    features = data.get("features")
    if not features:
        return None

    attrs = features[0].get("attributes", {})
    aw_mm_30 = attrs.get("AWmm30")
    if aw_mm_30 is None:
        return None

    suggested_field_capacity_vwc = ASSUMED_WILTING_POINT_VWC + (aw_mm_30 / ROOT_ZONE_DEPTH_MM)
    return {
        "field_capacity_vwc": round(suggested_field_capacity_vwc, 3),
        "root_zone_depth_mm": ROOT_ZONE_DEPTH_MM,
        "soil_name": attrs.get("LongSoilName"),
        "texture": attrs.get("SiblingTexture_Desc"),
        "drainage": attrs.get("SiblingDrainageCode_Desc"),
    }


def resolve_soil_params(lat, lon, explicit_root_zone_depth_mm=None, explicit_field_capacity_vwc=None, soil_type=None):
    """
    Resolves which root_zone_depth_mm/field_capacity_vwc to actually use,
    in priority order: explicit per-entry override > soil_type preset >
    live S-map lookup > generic default (handled by get_soil_moisture
    itself if both values returned here are None).
    """
    if explicit_root_zone_depth_mm is not None or explicit_field_capacity_vwc is not None:
        return explicit_root_zone_depth_mm, explicit_field_capacity_vwc

    if soil_type and soil_type in SOIL_TYPE_PRESETS:
        preset = SOIL_TYPE_PRESETS[soil_type]
        return preset["root_zone_depth_mm"], preset["field_capacity_vwc"]

    smap_result = lookup_smap_field_capacity(lat, lon)
    if smap_result:
        return smap_result["root_zone_depth_mm"], smap_result["field_capacity_vwc"]

    return None, None  # falls back to the generic default inside get_soil_moisture

# Apps Script web app URL (same one the webpage submits to) and the shared
# secret that authorises list/delete actions (set as a Script Property
# called SECRET in the Apps Script project, and as a GitHub repo secret here)
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL")
APPS_SCRIPT_SECRET = os.environ.get("APPS_SCRIPT_SECRET")

# Gmail (or any SMTP) credentials for sending subscriber emails. Use a Gmail
# "App Password", not your normal password: https://myaccount.google.com/apppasswords
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")

LOCATIONS_FILE = os.path.join(os.path.dirname(__file__), "locations.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

# yr.no (MET Norway) requires an identifying User-Agent with REAL contact
# info — generic/placeholder values get blocked with a 403. Put your actual
# email or a real domain here.
YR_USER_AGENT = "SpringNCheck/1.0 contact: nick@hoogs.nz"


def get_with_retry(url, params=None, headers=None, timeout=15, retries=2, backoff_seconds=3):
    """
    Simple retry wrapper for transient network issues (timeouts, momentary
    5xx errors) — APIs occasionally take longer than the timeout under load,
    and a quick retry is usually enough rather than failing the whole check.
    """
    last_exception = None
    for attempt in range(retries + 1):
        try:
            return requests.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            last_exception = e
            if attempt < retries:
                time.sleep(backoff_seconds)
    raise last_exception


def interpolate_10cm(t6, t18, weight=0.333):
    return t6 + (t18 - t6) * weight


def get_soil_temp_status(lat, lon):
    """
    Soil temp from Open-Meteo (6cm/18cm interpolated to 10cm), using DAILY
    MEAN (24h average) rather than a single 9am reading — the standard
    input for pasture growth models, since it smooths the diurnal swing.

    Returns:
      - growth_started: True once daily mean has held >= SOIL_TEMP_THRESHOLD_C
        for CONSECUTIVE_DAYS_REQUIRED days running (Frame's ryegrass growth-start rule)
      - accumulated_gdd: growing degree days summed from GDD_START_MONTH_DAY,
        using (daily_mean - GDD_BASE_TEMP_C) for any day above base temp
      - likely_strong_growth: accumulated_gdd >= GDD_THRESHOLD_20KGDM (rough estimate,
        needs calibration against actual farm-walk growth rates over a season)
    """
    from datetime import date

    today = date.today()
    year = today.year
    gdd_start = date.fromisoformat(f"{year}-{GDD_START_MONTH_DAY}")
    if gdd_start > today:
        gdd_start = date.fromisoformat(f"{year - 1}-{GDD_START_MONTH_DAY}")
    days_since_start = (today - gdd_start).days + 2  # +2 buffer for API lag

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=soil_temperature_6cm,soil_temperature_18cm"
        f"&past_days={min(days_since_start, 92)}&forecast_days=1"
        "&timezone=Pacific%2FAuckland"
    )
    resp = get_with_retry(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    hourly = data["hourly"]
    times = hourly["time"]
    t6_series = hourly["soil_temperature_6cm"]
    t18_series = hourly["soil_temperature_18cm"]

    # Group hourly readings into daily means
    daily_means = {}
    for i, t in enumerate(times):
        day = t[:10]
        temp10 = interpolate_10cm(t6_series[i], t18_series[i])
        daily_means.setdefault(day, []).append(temp10)
    daily_means = {d: sum(v) / len(v) for d, v in sorted(daily_means.items())}

    if len(daily_means) < CONSECUTIVE_DAYS_REQUIRED:
        raise ValueError("Not enough daily data to assess growth status.")

    days = list(daily_means.keys())
    temps = list(daily_means.values())

    # Consecutive-days-above-threshold check, ending on the most recent day
    consecutive = 0
    for t in reversed(temps):
        if t >= SOIL_TEMP_THRESHOLD_C:
            consecutive += 1
        else:
            break
    growth_started = consecutive >= CONSECUTIVE_DAYS_REQUIRED

    # GDD accumulation from gdd_start to most recent day
    accumulated_gdd = 0.0
    for d, t in daily_means.items():
        if d >= gdd_start.isoformat() and t > GDD_BASE_TEMP_C:
            accumulated_gdd += (t - GDD_BASE_TEMP_C)

    daily_means_list = list(zip(days, temps))

    return {
        "recent_temp": temps[-1],
        "as_of": days[-1],
        "consecutive_days_above_threshold": consecutive,
        "growth_started": growth_started,
        "accumulated_gdd": accumulated_gdd,
        "likely_strong_growth": accumulated_gdd >= GDD_THRESHOLD_20KGDM,
        "daily_means": daily_means_list[-21:],
    }


def get_yr_rain_forecast(lat, lon):
    """
    5-day total rainfall forecast, plus a per-day breakdown for charting.
    Tries yr.no (MET Norway) first, but falls back to Open-Meteo's ECMWF
    model if yr.no blocks the request — this happens often from CI/cloud
    datacenter IPs (like GitHub Actions runners) regardless of User-Agent
    correctness, since MET Norway blocks many shared cloud IP ranges as an
    anti-abuse measure.

    Returns a tuple: (total_rain_mm, source_name, daily_breakdown)
    where daily_breakdown is a list of (date_str, mm) for each of the next
    RAIN_FORECAST_DAYS days.
    """
    from datetime import datetime, timedelta

    try:
        url = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
        params = {"lat": lat, "lon": lon}
        resp = requests.get(
            url, params=params, headers={"User-Agent": YR_USER_AGENT}, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()

        timeseries = data["properties"]["timeseries"]
        total_rain = 0.0
        counted_hours = 0
        target_hours = RAIN_FORECAST_DAYS * 24
        daily_totals = {}

        for entry in timeseries:
            details = entry.get("data", {}).get("next_6_hours", {}).get("details", {})
            precip = details.get("precipitation_amount")
            if precip is not None:
                total_rain += precip
                counted_hours += 6
                day = entry["time"][:10]
                daily_totals[day] = daily_totals.get(day, 0.0) + precip
            if counted_hours >= target_hours:
                break

        breakdown = sorted(daily_totals.items())[:RAIN_FORECAST_DAYS]
        return total_rain, "yr.no (MET Norway)", breakdown
    except requests.RequestException as e:
        print(f"yr.no unavailable ({e}), falling back to Open-Meteo ECMWF.", file=sys.stderr)
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}&hourly=precipitation"
            f"&forecast_days={RAIN_FORECAST_DAYS}&models=ecmwf_ifs025"
            "&timezone=Pacific%2FAuckland"
        )
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        times = data["hourly"]["time"]
        precip_values = data["hourly"]["precipitation"]

        daily_totals = {}
        for t, p in zip(times, precip_values):
            day = t[:10]
            daily_totals[day] = daily_totals.get(day, 0.0) + p

        breakdown = sorted(daily_totals.items())[:RAIN_FORECAST_DAYS]
        total_rain = sum(mm for _, mm in breakdown)
        return total_rain, "Open-Meteo (ECMWF model)", breakdown


def rain_bar_chart(daily_breakdown):
    """Builds a simple ASCII bar chart of daily rainfall for a plain-text email."""
    if not daily_breakdown:
        return "(no daily breakdown available)"

    max_mm = max(mm for _, mm in daily_breakdown) or 1
    max_bar_width = 30
    lines = []
    for date_str, mm in daily_breakdown:
        bar_len = round((mm / max_mm) * max_bar_width) if max_mm > 0 else 0
        bar = "#" * bar_len
        lines.append(f"{date_str}  {bar.ljust(max_bar_width)} {mm:.1f}mm")
    return "\n".join(lines)


def categorize_soil_moisture(vwc):
    """
    vwc = volumetric water content, m3/m3. These are rough general bands,
    not soil-type-specific — Canterbury's shallow stony soils saturate at
    lower absolute values than deep silt/clay soils, so treat as a guide.
    """
    if vwc < 0.15:
        return "Dry"
    if vwc < 0.25:
        return "Moist"
    if vwc < 0.35:
        return "Wet"
    return "Saturated"


def get_soil_moisture(lat, lon, root_zone_depth_mm=None, field_capacity_vwc=None):
    """
    Depth-weighted root-zone soil moisture, plus the remaining buffer (mm)
    before the soil reaches field capacity. Open-Meteo's soil moisture comes
    in depth bands: 0-7cm, 7-28cm, 28-100cm. Defaults to the generic
    ROOT_ZONE_DEPTH_MM / FIELD_CAPACITY_VWC assumptions, but accepts
    per-farm overrides (e.g. sourced from an S-map factsheet lookup) for a
    more accurate result on a specific paddock's actual soil type.
    """
    root_zone_depth_mm = root_zone_depth_mm or ROOT_ZONE_DEPTH_MM
    field_capacity_vwc = field_capacity_vwc if field_capacity_vwc is not None else FIELD_CAPACITY_VWC

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=soil_moisture_0_to_7cm,soil_moisture_7_to_28cm,soil_moisture_28_to_100cm"
        "&past_days=1&forecast_days=1&timezone=Pacific%2FAuckland"
    )
    resp = get_with_retry(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    l1 = data["hourly"]["soil_moisture_0_to_7cm"]
    l2 = data["hourly"]["soil_moisture_7_to_28cm"]
    l3 = data["hourly"]["soil_moisture_28_to_100cm"]

    last_idx = None
    for i in range(len(l1) - 1, -1, -1):
        if l1[i] is not None and l2[i] is not None and l3[i] is not None:
            last_idx = i
            break
    if last_idx is None:
        return None

    root_zone_depth_cm = root_zone_depth_mm / 10
    w1 = min(7, root_zone_depth_cm)
    w2 = min(21, max(0, root_zone_depth_cm - 7))
    w3 = max(0, root_zone_depth_cm - 28)

    root_zone_vwc = (w1 * l1[last_idx] + w2 * l2[last_idx] + w3 * l3[last_idx]) / root_zone_depth_cm
    deficit_mm = max(0.0, (field_capacity_vwc - root_zone_vwc) * root_zone_depth_mm)

    return {
        "vwc": root_zone_vwc,
        "category": categorize_soil_moisture(root_zone_vwc),
        "deficit_mm": deficit_mm,
    }


def check_location(name, lat, lon, root_zone_depth_mm=None, field_capacity_vwc=None):
    status = get_soil_temp_status(lat, lon)
    rain_forecast, rain_source, rain_breakdown = get_yr_rain_forecast(lat, lon)

    try:
        soil_moisture = get_soil_moisture(lat, lon, root_zone_depth_mm, field_capacity_vwc)
    except requests.RequestException:
        soil_moisture = None  # informational only; never blocks the check

    rain_threshold_mm = (
        soil_moisture["deficit_mm"] + RAIN_SAFETY_MARGIN_MM
        if soil_moisture else RAIN_RISK_MM_5DAY  # fallback if data unavailable
    )

    conditions_met = (
        status["growth_started"]
        and rain_forecast < rain_threshold_mm
    )

    return {
        "name": name,
        "soil_temp_10cm": round(status["recent_temp"], 1),
        "consecutive_days_above_threshold": status["consecutive_days_above_threshold"],
        "growth_started": status["growth_started"],
        "accumulated_gdd": round(status["accumulated_gdd"], 1),
        "likely_strong_growth_20kgdm": status["likely_strong_growth"],
        "rain_forecast_5day_mm": round(rain_forecast, 1),
        "rain_source": rain_source,
        "rain_breakdown": rain_breakdown,
        "soil_moisture": soil_moisture,
        "rain_threshold_mm": round(rain_threshold_mm, 1),
        "conditions_met": conditions_met,
        "as_of": status["as_of"],
    }


def send_push(title, message):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set, skipping push. Message was:")
        print(title, "-", message)
        return
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": "high", "Tags": "seedling"},
        timeout=10,
    )


def send_email(to_address, subject, body):
    if not (SMTP_USER and SMTP_PASSWORD):
        print(f"SMTP not configured, skipping email to {to_address}. Message was:")
        print(subject, "-", body)
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = to_address
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [to_address], msg.as_string())


def load_subscribers():
    """Fetches the current subscriber list from the Apps Script web app."""
    if not (APPS_SCRIPT_URL and APPS_SCRIPT_SECRET):
        return []
    resp = requests.post(
        APPS_SCRIPT_URL,
        json={"action": "list", "secret": APPS_SCRIPT_SECRET},
        timeout=15,
    )
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        print(f"Error listing subscribers: {result.get('error')}", file=sys.stderr)
        return []
    return result.get("subscribers", [])


def delete_subscriber(row_number):
    """Removes a subscriber's row after they've been notified."""
    resp = requests.post(
        APPS_SCRIPT_URL,
        json={"action": "delete", "secret": APPS_SCRIPT_SECRET, "rowNumbers": [row_number]},
        timeout=15,
    )
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "Unknown error deleting subscriber."))


def process_subscribers():
    subscribers = load_subscribers()
    for sub in subscribers:
        try:
            # Soil values were looked up ONCE at signup time by the webpage
            # and stored in the Sheet — using them here avoids an extra
            # live S-map API call on every single daily run.
            result = check_location(
                sub["name"], sub["lat"], sub["lon"],
                root_zone_depth_mm=sub.get("rootZoneDepthMm"),
                field_capacity_vwc=sub.get("fieldCapacityVwc"),
            )
        except Exception as e:
            print(f"Error checking subscriber {sub['name']}: {e}", file=sys.stderr)
            continue

        print(json.dumps(result))
        if not result["conditions_met"]:
            continue

        subject = f"Spring N: conditions met at {sub['name']}"
        body = (
            f"Conditions look right for spring N application at {sub['name']}.\n\n"
            f"This was a one-time notification — you've now been taken off the list and "
            f"won't get any further emails unless you sign up again from the page.\n\n"
            f"Soil temp (10cm daily mean): {result['soil_temp_10cm']}\u00b0C\n"
            f"Growth started: {result['consecutive_days_above_threshold']} consecutive days \u22655.5\u00b0C\n"
            f"Accumulated GDD: {result['accumulated_gdd']} / {GDD_THRESHOLD_20KGDM}\n"
            f"Rain forecast (5 days): {result['rain_forecast_5day_mm']}mm\n"
            f"Rain forecast source: {result['rain_source']}\n"
            f"Rain threshold used: {result['rain_threshold_mm']}mm\n"
            + (
                f"Soil moisture (300mm root zone): {result['soil_moisture']['category']} "
                f"({result['soil_moisture']['vwc'] * 100:.0f}% VWC, "
                f"~{result['soil_moisture']['deficit_mm']:.0f}mm buffer remaining)\n"
                if result.get("soil_moisture") else ""
            )
            + f"\n"
            f"Rainfall by day:\n"
            f"{rain_bar_chart(result['rain_breakdown'])}\n"
            f"\n"
            f"---\n"
            f"How this was worked out:\n"
            f"\n"
            f"Soil temp (10cm daily mean): Interpolated from Open-Meteo's modelled "
            f"6cm and 18cm soil temperature layers, averaged over each full day "
            f"(rather than a single time-of-day reading) to smooth out the normal "
            f"day/night swing.\n"
            f"\n"
            f"Growth started: Ryegrass is taken to have started active spring growth "
            f"once the 10cm daily mean has held at or above 5.5\u00b0C for 5 consecutive "
            f"days, following Frame's commonly-cited soil temperature threshold for "
            f"ryegrass growth onset.\n"
            f"\n"
            f"Accumulated GDD: Growing degree days, summed as (daily mean - 5\u00b0C) for "
            f"every day above that base temperature, starting 1 June. This is a "
            f"secondary indicator of how much growth has likely accumulated. The "
            f"150 GDD threshold shown is a starting estimate for when growth "
            f"typically exceeds ~20kgDM/ha/day on Canterbury dairy pasture, and "
            f"should be checked against actual farm-walk growth rates over a "
            f"season and adjusted if needed.\n"
            f"\n"
            f"Rain forecast: Total rainfall expected over the next 5 days, used as a "
            f"hold-off check against leaching/runoff risk shortly after application. "
            f"Sourced from yr.no (MET Norway) where reachable, otherwise from "
            f"Open-Meteo's ECMWF model as a fallback \u2014 the source actually used for "
            f"this check is shown above.\n"
            f"\n"
            f"Soil moisture: Depth-weighted across Open-Meteo's modelled layers to "
            f"represent a 300mm root zone (the conservative end of ryegrass's typical "
            f"300-400mm root depth). Converted into a remaining buffer (mm) before the "
            f"soil reaches field capacity (assumed 30% VWC for Canterbury's typical "
            f"shallow stony soils -- adjust if you have better local data). The rain "
            f"threshold used to decide go/hold-off is that buffer plus a small safety "
            f"margin, rather than a flat number -- a drier soil tolerates more forecast "
            f"rain, a wetter soil triggers hold-off sooner.\n"
            f"\n"
            f"These are modelled estimates, not a substitute for what you see and "
            f"feel in the paddock.\n"
        )

        if sub["email"]:
            try:
                send_email(sub["email"], subject, body)
                delete_subscriber(sub["rowNumber"])
                print(f"Notified and removed {sub['email']} ({sub['name']}).")
            except Exception as e:
                print(f"Error emailing/removing {sub['email']}: {e}", file=sys.stderr)


def main():
    if os.path.exists(LOCATIONS_FILE):
        with open(LOCATIONS_FILE) as f:
            locations = json.load(f)

        triggered = []
        for name, coords in locations.items():
            try:
                root_zone_depth_mm, field_capacity_vwc = resolve_soil_params(
                    coords["lat"], coords["lon"],
                    explicit_root_zone_depth_mm=coords.get("root_zone_depth_mm"),
                    explicit_field_capacity_vwc=coords.get("field_capacity_vwc"),
                    soil_type=coords.get("soil_type"),
                )

                result = check_location(
                    name, coords["lat"], coords["lon"],
                    root_zone_depth_mm=root_zone_depth_mm,
                    field_capacity_vwc=field_capacity_vwc,
                )
                print(json.dumps(result))
                if result["conditions_met"]:
                    triggered.append(result)
            except Exception as e:
                print(f"Error checking {name}: {e}", file=sys.stderr)

        if triggered:
            lines = [
                f"{r['name']}: {r['soil_temp_10cm']}\u00b0C, growth started "
                f"({r['consecutive_days_above_threshold']} days), "
                f"GDD {r['accumulated_gdd']}"
                + (" — likely >20kgDM/ha/day" if r['likely_strong_growth_20kgdm'] else "")
                + f", rain {r['rain_forecast_5day_mm']}mm/5day ({r['rain_source']})"
                for r in triggered
            ]
            send_push("Spring N: conditions met", "\n".join(lines))
        else:
            print("No locations.json entries met conditions today.")

    process_subscribers()


if __name__ == "__main__":
    main()
