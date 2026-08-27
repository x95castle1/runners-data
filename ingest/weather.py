"""Historical weather for a run, hour by hour.

Apple records one temperature and humidity per workout, taken at the start. On a
four-hour long run that single figure is close to useless -- the morning warms up
five degrees underneath you and the wind swings around. This fills in the rest
from Open-Meteo's archive, which needs no key and no account.

Two things worth being honest about:

* The data is **hourly**. Splits interpolate between readings, which makes the
  numbers move smoothly, but it does not invent resolution -- on a forty-minute
  run every split sees essentially the same weather.
* Wind is modelled at ten metres over open ground. The headwind figure is
  directionally right, not street-level accurate.

Cross-checked against what the watch recorded: Apple logged 64.4F/95% at 04:54 on
2026-08-22 where this source says 63.5F/95% at 05:00.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

FIELDS = ("temperature_2m,apparent_temperature,relative_humidity_2m,"
          "dew_point_2m,wind_speed_10m,wind_direction_10m,precipitation")

COLUMNS = ["temp_f", "apparent_f", "humidity_pct", "dew_point_f",
           "wind_mph", "wind_dir_deg", "precip_in"]

_RESPONSE_KEYS = ["temperature_2m", "apparent_temperature", "relative_humidity_2m",
                  "dew_point_2m", "wind_speed_10m", "wind_direction_10m",
                  "precipitation"]


class WeatherUnavailable(RuntimeError):
    """The lookup failed. Never fatal -- the import carries on without it."""


def place_key(lat: float, lon: float) -> str:
    """Round to a tenth of a degree, roughly eleven kilometres.

    That is the resolution of the finest grid this data comes from, so anything
    finer is false precision -- and eight separate cache cells for one town means
    eight lookups returning the same weather. It also keeps the precision of
    someone's front door out of an outbound request.
    """
    return f"{lat:.1f},{lon:.1f}"


def _request(url: str, params: dict, timeout: int) -> dict:
    full = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(full, timeout=timeout) as response:
        return json.load(response)


def fetch_day(lat: float, lon: float, day: str, *, timezone_name: str = "auto",
              timeout: int = 20) -> list[dict]:
    """Hourly rows for one calendar day at one place, in local time."""
    params = {
        "latitude": round(lat, 1), "longitude": round(lon, 1),
        "start_date": day, "end_date": day, "hourly": FIELDS,
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        "precipitation_unit": "inch", "timezone": timezone_name,
    }
    transient = None
    for attempt in range(3):
        try:
            payload = _request(ARCHIVE_URL, params, timeout)
            hourly = payload.get("hourly") or {}
            if not any(v is not None for v in hourly.get("temperature_2m") or []):
                # The archive lags a little; the forecast endpoint keeps recent days.
                payload = _request(FORECAST_URL, params, timeout)
                hourly = payload.get("hourly") or {}
            break
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as problem:
            # TLS handshakes to this host time out occasionally; a retry clears it.
            transient = problem
            time.sleep(1.5 * (attempt + 1))
    else:
        raise WeatherUnavailable(str(transient)) from transient

    times = hourly.get("time") or []
    if not times:
        raise WeatherUnavailable(f"no hourly data returned for {day}")

    rows = []
    for i, stamp in enumerate(times):
        row = {"hour_ts": stamp}
        for column, key in zip(COLUMNS, _RESPONSE_KEYS):
            values = hourly.get(key) or []
            row[column] = values[i] if i < len(values) else None
        rows.append(row)
    return rows


def cached_days(conn: sqlite3.Connection, place: str) -> set[str]:
    return {r[0][:10] for r in conn.execute(
        "SELECT hour_ts FROM weather_hours WHERE place = ?", (place,))}


def store(conn: sqlite3.Connection, place: str, rows: list[dict]) -> int:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.executemany(
        f"INSERT OR REPLACE INTO weather_hours (place, hour_ts, {', '.join(COLUMNS)},"
        " fetched_at) VALUES (:place, :hour_ts,"
        f" {', '.join(':' + c for c in COLUMNS)}, :fetched_at)",
        [{**row, "place": place, "fetched_at": stamp} for row in rows])
    conn.commit()
    return len(rows)


def backfill(conn: sqlite3.Connection, targets: list[dict], *,
             pause: float = 0.3, log=print) -> dict:
    """Fetch every (place, day) these runs need that isn't cached already.

    `targets` are dicts with id, date, lat, lon. Failures are collected and
    reported rather than raised -- weather is a garnish, and losing the network
    must never cost you an import.
    """
    wanted: dict[tuple[str, str], None] = {}
    for run in targets:
        wanted[(place_key(run["lat"], run["lon"]), run["date"])] = None

    have = {}
    fetched = skipped = hours = 0
    failures: list[str] = []
    for place, day in wanted:
        if place not in have:
            have[place] = cached_days(conn, place)
        if day in have[place]:
            skipped += 1
            continue
        lat, lon = (float(x) for x in place.split(","))
        try:
            rows = fetch_day(lat, lon, day)
        except WeatherUnavailable as problem:
            failures.append(f"{day} at {place}: {problem}")
            continue
        hours += store(conn, place, rows)
        have[place].add(day)
        fetched += 1
        if pause:
            time.sleep(pause)   # be a polite guest on a free service

    for run in targets:
        conn.execute("UPDATE runs SET weather_place = ? WHERE id = ?",
                     (place_key(run["lat"], run["lon"]), run["id"]))
    conn.commit()
    return {"runs": len(targets), "fetched": fetched, "cached": skipped,
            "hours": hours, "failures": failures}
