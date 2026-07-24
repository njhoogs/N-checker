#!/usr/bin/env python3
"""
fetch_probe_data.py — pull real soil-probe daily means from Harvest and publish
them as static JSON for spring-n-check.html to read.

Runs in GitHub Actions on a schedule. The Harvest API key lives in repo secrets
and never reaches the browser; this job writes probe-data/<slug>.json, commits
it, and the page fetches that file instead of Open-Meteo where it exists.

Why this exists: Open-Meteo's modelled soil temperature carries a location-
specific bias of inconsistent sign and magnitude (N-checker handover section 9,
plus the Kintore probe comparison: Wainui +2.0 d, Katoa +8.4 d, range -16 to
+42 d). No per-site correction generalises. Where a farm has real probes,
reading them directly sidesteps the problem entirely.

Config lives in probe-sites.json:

  {
    "kintore-wainui": {
      "label": "Kintore (Wainui)",
      "site_id": 1018,
      "traces": [61000, 60999, 216189, 447112],
      "lat": -43.9481,
      "lon": 171.3941
    }
  }

`traces` are 10cm ("Soil Temp 100mm") traces for the blocks that represent the
walked grazing platform. Every trace listed must be verified by hand before it
goes in - a mis-wired input is invisible in the output (see the Katoa ET trace,
which had wind connected to a logger battery voltage and still returned
plausible numbers).

Usage:
  HARVEST_API_KEY=xxx python fetch_probe_data.py
  HARVEST_API_KEY=xxx python fetch_probe_data.py --site kintore-wainui --dry-run
"""

import argparse
import datetime
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://live.harvest.com/api.php"
KEY = os.environ.get("HARVEST_API_KEY", "")

CONFIG = Path("probe-sites.json")
OUTDIR = Path("probe-data")

# Season window. The rule needs 5 consecutive days plus the 15-July floor, so
# season-to-date from 1 June is ample. Keeps the published file small.
SEASON_START_MONTH = 6
SEASON_START_DAY = 1

# Refetch the trailing window each run rather than trusting the cache - Harvest
# can backfill a late-reporting logger, and a stale gap would silently shorten a
# consecutive run.
REFETCH_TRAILING_DAYS = 10

# ---------------------------------------------------------------------------
# QC — the same two rules, and the same constants, as
# harvest_join.py's qc_soil_temperature(). That function is the original;
# backtest_forecaster.py ported it, and this is the third copy.
#
# Do not let these three diverge. If a rule changes, change it in all three,
# or the N-checker will disagree with the calibration set about what the
# soil temperature was on a given day.
# ---------------------------------------------------------------------------

SOIL_TEMP_MIN, SOIL_TEMP_MAX = -5.0, 40.0     # physically implausible outside this
SOIL_TEMP_DIFF_MAX = 5.0                      # probe-vs-probe disagreement, degC
SOIL_TEMP_ROLLING_DAYS = 15                   # trailing median window

# Refuse to publish if the data is older than this - a silently stale file is
# worse than no file, because the page would treat it as current.
MAX_STALENESS_DAYS = 3


def api(command, params, tries=4):
    q = dict(params)
    q.update(output_type="application/json", command_type=command, api_key=KEY)
    url = API + "?" + urllib.parse.urlencode(q, safe="[],")
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 ** attempt)


def summary_for(date_str, trace_ids):
    """{trace_id: average} for one calendar day, site-local midnight to midnight."""
    out = {}
    j = api("get_summary_data",
            {"trace_ids": "[" + ",".join(map(str, trace_ids)) + "]",
             "date": date_str + " 00:00:00"})
    traces = j.get("traces") or j.get("data") or {}
    if isinstance(traces, dict):
        for k, t in traces.items():
            tid = int(t.get("trace_id", k))
            day = t.get("day") or t.get("summary") or t
            v = day.get("average")
            try:
                v = float(v)
                if v == v:
                    out[tid] = v
            except (TypeError, ValueError):
                pass
    return out


def season_start(today):
    """1 June of the current season (previous year if we're before June)."""
    y = today.year if (today.month, today.day) >= (SEASON_START_MONTH, SEASON_START_DAY) else today.year - 1
    return datetime.date(y, SEASON_START_MONTH, SEASON_START_DAY)


def qc_day(readings, history):
    """
    Apply both QC rules to one day's probe readings.

    readings: {trace_id: value}
    history:  list of recently accepted values, any probe, for the median

    Returns (accepted {trace_id: value}, rejections [(trace_id, rule)]).
    """
    accepted, rejected = {}, []

    # Rule 1 - range
    for tid, v in readings.items():
        if v < SOIL_TEMP_MIN or v > SOIL_TEMP_MAX:
            rejected.append((tid, "range"))
        else:
            accepted[tid] = v

    # Rule 2 - probe disagreement. Only meaningful with 2+ survivors and some
    # history to judge against. Drop the reading furthest from the trailing
    # median, keep the rest, repeat while the spread is still too wide.
    while len(accepted) >= 2 and history:
        vals = list(accepted.values())
        if max(vals) - min(vals) <= SOIL_TEMP_DIFF_MAX:
            break
        med = statistics.median(history)
        worst = max(accepted, key=lambda t: abs(accepted[t] - med))
        del accepted[worst]
        rejected.append((worst, "diff"))

    return accepted, rejected


def build_site(slug, cfg, dry_run=False):
    traces = cfg["traces"]
    today = datetime.date.today()
    start = season_start(today)

    outfile = OUTDIR / f"{slug}.json"
    existing = {}
    if outfile.exists():
        try:
            prev = json.loads(outfile.read_text())
            existing = {d["date"]: d for d in prev.get("dailyMeans", [])}
        except Exception:
            existing = {}

    # Refetch the trailing window even if cached, in case of backfill.
    refetch_from = today - datetime.timedelta(days=REFETCH_TRAILING_DAYS)

    daily, qc_log = [], {"range": 0, "diff": 0}
    history = []          # trailing accepted values for the median
    hist_dates = []

    d = start
    fetched = 0
    while d <= today:
        ds = d.isoformat()
        cached = existing.get(ds)

        if cached and d < refetch_from and cached.get("mean") is not None:
            daily.append({"date": ds, "mean": cached["mean"], "n": cached.get("n")})
            history.append(cached["mean"])
            hist_dates.append(d)
        else:
            try:
                readings = summary_for(ds, traces)
                fetched += 1
                if fetched % 20 == 0:
                    time.sleep(1)          # stay well inside 200 req/min
            except Exception as e:
                print(f"  !! {ds}: {type(e).__name__} {e}", file=sys.stderr)
                readings = {}

            accepted, rejected = qc_day(readings, history)
            for _, rule in rejected:
                qc_log[rule] += 1

            if accepted:
                mean = round(sum(accepted.values()) / len(accepted), 2)
                daily.append({"date": ds, "mean": mean, "n": len(accepted)})
                history.append(mean)
                hist_dates.append(d)
            # A day with no usable reading is simply absent. The page treats a
            # gap as a break in the consecutive run, which is the safe reading -
            # better to under-call growth start than over-call it.

        # trim the rolling window
        cutoff = d - datetime.timedelta(days=SOIL_TEMP_ROLLING_DAYS)
        while hist_dates and hist_dates[0] < cutoff:
            hist_dates.pop(0)
            history.pop(0)

        d += datetime.timedelta(days=1)

    if not daily:
        print(f"  {slug}: no usable data, not writing", file=sys.stderr)
        return None

    last = datetime.date.fromisoformat(daily[-1]["date"])
    staleness = (today - last).days

    payload = {
        "slug": slug,
        "label": cfg.get("label", slug),
        "source": "harvest-probe",
        "site_id": cfg["site_id"],
        "traces": traces,
        "lat": cfg.get("lat"),
        "lon": cfg.get("lon"),
        "depth_mm": cfg.get("depth_mm", 100),
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "latest_date": daily[-1]["date"],
        "staleness_days": staleness,
        "stale": staleness > MAX_STALENESS_DAYS,
        "qc_rejected": qc_log,
        "dailyMeans": daily,
    }

    print(f"  {slug}: {len(daily)} days, {start} -> {daily[-1]['date']}, "
          f"{fetched} fetched, QC {qc_log['range']} range / {qc_log['diff']} diff"
          + (f"  ** STALE by {staleness} d **" if payload["stale"] else ""))

    if dry_run:
        print(json.dumps({k: v for k, v in payload.items() if k != "dailyMeans"}, indent=2))
        return payload

    OUTDIR.mkdir(exist_ok=True)
    outfile.write_text(json.dumps(payload, indent=1))
    return payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", help="only this slug")
    ap.add_argument("--dry-run", action="store_true", help="don't write files")
    args = ap.parse_args()

    if not KEY:
        sys.exit("HARVEST_API_KEY not set.")
    if not CONFIG.exists():
        sys.exit(f"{CONFIG} not found.")

    sites = json.loads(CONFIG.read_text())
    if args.site:
        if args.site not in sites:
            sys.exit(f"unknown site {args.site!r}; have {list(sites)}")
        sites = {args.site: sites[args.site]}

    print(f"fetching probe data for {len(sites)} site(s)")
    any_stale = False
    for slug, cfg in sites.items():
        p = build_site(slug, cfg, dry_run=args.dry_run)
        if p and p["stale"]:
            any_stale = True

    if any_stale:
        print("\nWARNING: at least one site is stale. The page will fall back to "
              "Open-Meteo for those and say so.", file=sys.stderr)


if __name__ == "__main__":
    main()
