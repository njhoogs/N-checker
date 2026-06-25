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
from email.mime.text import MIMEText

import requests

SOIL_TEMP_THRESHOLD_C = 5.5      # growth-start threshold (Frame; ryegrass begins growth at this soil temp)
CONSECUTIVE_DAYS_REQUIRED = 5    # must hold at/above threshold for this many consecutive days
GDD_BASE_TEMP_C = 5.0            # base temp for growing degree day accumulation (NZ cool-season pasture convention)
GDD_START_MONTH_DAY = "06-01"    # accumulate GDD from 1 June each year (adjust to your local season start)
GDD_THRESHOLD_20KGDM = 150       # STARTING ESTIMATE ONLY — calibrate against your own farm-walk growth data
RAIN_FORECAST_DAYS = 5
RAIN_RISK_MM_5DAY = 40.0

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
    resp = requests.get(url, timeout=15)
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
    5-day total rainfall forecast. Tries yr.no (MET Norway) first, but falls
    back to Open-Meteo's ECMWF model if yr.no blocks the request — this
    happens often from CI/cloud datacenter IPs (like GitHub Actions runners)
    regardless of User-Agent correctness, since MET Norway blocks many
    shared cloud IP ranges as an anti-abuse measure.

    Returns a tuple: (total_rain_mm, source_name)
    """
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

        for entry in timeseries:
            details = entry.get("data", {}).get("next_6_hours", {}).get("details", {})
            precip = details.get("precipitation_amount")
            if precip is not None:
                total_rain += precip
                counted_hours += 6
            if counted_hours >= target_hours:
                break

        return total_rain, "yr.no (MET Norway)"
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
        return sum(data["hourly"]["precipitation"]), "Open-Meteo (ECMWF model)"


def check_location(name, lat, lon):
    status = get_soil_temp_status(lat, lon)
    rain_forecast, rain_source = get_yr_rain_forecast(lat, lon)

    conditions_met = (
        status["growth_started"]
        and rain_forecast < RAIN_RISK_MM_5DAY
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
            result = check_location(sub["name"], sub["lat"], sub["lon"])
        except Exception as e:
            print(f"Error checking subscriber {sub['name']}: {e}", file=sys.stderr)
            continue

        print(json.dumps(result))
        if not result["conditions_met"]:
            continue

        subject = f"Spring N: conditions met at {sub['name']}"
        body = (
            f"Conditions look right for spring N application at {sub['name']}.\n\n"
            f"Soil temp (10cm daily mean): {result['soil_temp_10cm']}\u00b0C\n"
            f"Growth started: {result['consecutive_days_above_threshold']} consecutive days \u22655.5\u00b0C\n"
            f"Accumulated GDD: {result['accumulated_gdd']} / {GDD_THRESHOLD_20KGDM}\n"
            f"Rain forecast (5 days): {result['rain_forecast_5day_mm']}mm\n"
            f"Rain forecast source: {result['rain_source']}\n"
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
                result = check_location(name, coords["lat"], coords["lon"])
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
