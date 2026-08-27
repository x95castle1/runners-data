"""Aggregations over the runs table.

Everything takes an optional date window so the dashboard, the API and the table
view can all be scoped by the same filter row.

Runs carry a status: `completed` (a time was recorded), `skipped` (it was on the
plan and the time was entered as zero) or `planned` (still ahead). Every total
here counts completed runs only unless asked otherwise -- a training plan you
haven't run yet shouldn't inflate your mileage.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

# Monday-of-week for an ISO date column, in SQLite: %w is Sun=0..Sat=6, so the
# offset back to Monday is (%w + 6) % 7 days.
WEEK_START = "date(date, '-' || ((CAST(strftime('%w', date) AS INTEGER) + 6) % 7) || ' days')"


COMPLETED = "completed"


def _window(start: str | None, end: str | None,
            workout_type: str | None = None,
            status: str | None = COMPLETED) -> tuple[str, list]:
    clauses, params = [], []
    if status and status != "all":
        clauses.append("status = ?")
        params.append(status)
    if start:
        clauses.append("date >= ?")
        params.append(start)
    if end:
        clauses.append("date <= ?")
        params.append(end)
    if workout_type:
        clauses.append("workout_type = ?")
        params.append(workout_type)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def summary(conn: sqlite3.Connection, start=None, end=None, workout_type=None,
            status=COMPLETED) -> dict:
    where, params = _window(start, end, workout_type, status)
    row = conn.execute(f"""
        SELECT COUNT(*)                AS runs,
               COALESCE(SUM(distance_mi), 0)  AS miles,
               COALESCE(SUM(duration_sec), 0) AS seconds,
               MAX(distance_mi)        AS longest,
               MIN(date)               AS first_date,
               MAX(date)               AS last_date,
               AVG(avg_hr)             AS avg_hr
        FROM runs{where}
    """, params).fetchone()

    result = dict(row)
    # Pace is total time over total distance, not the mean of per-run paces --
    # otherwise a 1-mile shakeout weighs as much as a 20-miler.
    result["avg_pace"] = (result["seconds"] / result["miles"]) if result["miles"] else None
    result["avg_hr"] = round(result["avg_hr"]) if result["avg_hr"] else None
    return result


def week_summary(conn: sqlite3.Connection, offset_weeks: int = 0) -> dict:
    """This training week (Mon-Sun), or a previous one via offset_weeks=1."""
    today = date.today()
    monday = today - timedelta(days=today.weekday()) - timedelta(weeks=offset_weeks)
    sunday = monday + timedelta(days=6)
    result = summary(conn, monday.isoformat(), sunday.isoformat())
    result["week_start"] = monday.isoformat()
    result["week_end"] = sunday.isoformat()
    # What the plan still has queued for the rest of this week.
    remaining = conn.execute(
        "SELECT COUNT(*) AS runs, COALESCE(SUM(distance_mi), 0) AS miles"
        " FROM runs WHERE status IN ('planned', 'skipped') AND date BETWEEN ? AND ?",
        (monday.isoformat(), sunday.isoformat())).fetchone()
    result["planned_runs"] = remaining["runs"]
    result["planned_miles"] = round(remaining["miles"], 1)
    result["plan_total"] = round(result["miles"] + remaining["miles"], 1)
    return result


def weekly(conn: sqlite3.Connection, weeks: int = 16, since: str | None = None) -> list[dict]:
    """Mileage per training week, oldest first, with empty weeks filled in."""
    rows = conn.execute(f"""
        SELECT {WEEK_START} AS week_start,
               SUM(distance_mi)  AS miles,
               SUM(duration_sec) AS seconds,
               COUNT(*)          AS runs
        FROM runs
        WHERE status = 'completed' AND (? IS NULL OR date >= ?)
        GROUP BY week_start
        ORDER BY week_start
    """, (since, since)).fetchall()
    if not rows:
        return []

    by_week = {r["week_start"]: dict(r) for r in rows}
    first = datetime.fromisoformat(rows[0]["week_start"]).date()
    today = date.today()
    last = max(datetime.fromisoformat(rows[-1]["week_start"]).date(),
               today - timedelta(days=today.weekday()))

    out, cursor = [], first
    while cursor <= last:
        key = cursor.isoformat()
        week = by_week.get(key, {"week_start": key, "miles": 0, "seconds": 0, "runs": 0})
        week["miles"] = round(week["miles"] or 0, 1)
        week["avg_pace"] = (week["seconds"] / week["miles"]) if week["miles"] else None
        out.append(week)
        cursor += timedelta(days=7)
    return out[-weeks:] if weeks else out


def daily_load(conn: sqlite3.Connection, window: int = 7, days: int = 180,
               since: str | None = None) -> list[dict]:
    """Rolling N-day mileage -- the training-load curve. Every calendar day gets
    a point, including rest days, so a taper actually looks like a taper."""
    rows = conn.execute("""
        SELECT date, SUM(distance_mi) AS miles
        FROM runs WHERE status = 'completed' AND (? IS NULL OR date >= ?)
        GROUP BY date ORDER BY date
    """, (since, since)).fetchall()
    if not rows:
        return []

    by_day = {r["date"]: (r["miles"] or 0) for r in rows}
    start = datetime.fromisoformat(rows[0]["date"]).date()
    end = max(datetime.fromisoformat(rows[-1]["date"]).date(), date.today())

    series, cursor, buffer = [], start, []
    while cursor <= end:
        buffer.append(by_day.get(cursor.isoformat(), 0))
        if len(buffer) > window:
            buffer.pop(0)
        series.append({"date": cursor.isoformat(), "miles": round(sum(buffer), 1)})
        cursor += timedelta(days=1)
    return series[-days:] if days else series


def pace_series(conn: sqlite3.Connection, min_distance: float = 1.0,
                since: str | None = None) -> list[dict]:
    rows = conn.execute("""
        SELECT date, pace_sec_per_mi, distance_mi, workout_type, id
        FROM runs
        WHERE status = 'completed' AND pace_sec_per_mi IS NOT NULL AND distance_mi >= ?
          AND (? IS NULL OR date >= ?)
        ORDER BY date
    """, (min_distance, since, since)).fetchall()
    return [dict(r) for r in rows]


def pace_trend(rows: list[dict]) -> dict | None:
    """Least-squares fit through pace over time.

    The slope is the thing worth having: seconds per mile gained or lost per
    month. `fit` is how much of the scatter the line accounts for -- pace swings
    with workout type as much as with fitness, so a modest figure is normal and
    a low one means the line is describing the mix of sessions more than any
    trend in form.
    """
    from datetime import date

    points = [(date.fromisoformat(r["date"]), r["pace_sec_per_mi"])
              for r in rows if r.get("pace_sec_per_mi")]
    if len(points) < 5:
        return None

    origin = points[0][0]
    xs = [(day - origin).days for day, _ in points]
    ys = [pace for _, pace in points]
    span = xs[-1] - xs[0]
    if span <= 0:
        return None

    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    variance = sum((x - mean_x) ** 2 for x in xs)
    if not variance:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / variance
    intercept = mean_y - slope * mean_x

    total = sum((y - mean_y) ** 2 for y in ys)
    residual = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    fit = (1 - residual / total) if total else 0.0

    return {
        "start_date": points[0][0].isoformat(),
        "end_date": points[-1][0].isoformat(),
        "start_pace": intercept + slope * xs[0],
        "end_pace": intercept + slope * xs[-1],
        # Negative is faster, so flip the sign: positive means improving.
        "seconds_per_month": -slope * 30.44,
        "fit": fit,
        "runs": n,
        "days": span,
    }


def personal_bests(conn: sqlite3.Connection, since: str | None = None) -> dict:
    def one(sql, params=()):
        row = conn.execute(sql + " ", (since, since, *params)).fetchone()
        return dict(row) if row else None

    return {
        "longest_run": one("""
            SELECT id, date, distance_mi, duration_sec, pace_sec_per_mi
            FROM runs WHERE status = 'completed' AND (? IS NULL OR date >= ?)
              AND distance_mi IS NOT NULL
            ORDER BY distance_mi DESC LIMIT 1"""),
        # Restricted to >=3mi so a fast quarter-mile stride doesn't take the crown.
        "fastest_pace": one("""
            SELECT id, date, distance_mi, duration_sec, pace_sec_per_mi
            FROM runs WHERE status = 'completed' AND (? IS NULL OR date >= ?)
              AND pace_sec_per_mi IS NOT NULL AND distance_mi >= 3
            ORDER BY pace_sec_per_mi ASC LIMIT 1"""),
        "biggest_week": one(f"""
            SELECT {WEEK_START} AS week_start, SUM(distance_mi) AS miles, COUNT(*) AS runs
            FROM runs WHERE status = 'completed' AND (? IS NULL OR date >= ?)
            GROUP BY week_start ORDER BY miles DESC LIMIT 1"""),
    }


def workout_types(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("""
        SELECT DISTINCT workout_type FROM runs
        WHERE workout_type IS NOT NULL AND TRIM(workout_type) <> ''
        ORDER BY workout_type
    """).fetchall()
    return [r["workout_type"] for r in rows]


def type_slots(conn: sqlite3.Connection, limit: int = 5) -> dict[str, int]:
    """workout type -> categorical color slot.

    Assigned alphabetically over the whole dataset, so a type keeps its color on
    every page and filtering never repaints the survivors. Slots stop at the five
    validated hues -- anything past that gets a neutral dot rather than a recycled
    hue, since the badge text carries the identity anyway. "Run" is the loader's
    catch-all for a workout it couldn't classify, so it takes the neutral dot
    first and leaves the colors for types that mean something.
    """
    named = [t for t in workout_types(conn) if t.lower() != "run"]
    return {name: i + 1 for i, name in enumerate(named[:limit])}


def list_runs(conn, *, start=None, end=None, workout_type=None, status=COMPLETED,
              sort="date", direction="desc", limit=500) -> list[dict]:
    allowed = {"date", "distance_mi", "duration_sec", "pace_sec_per_mi",
               "avg_hr", "elevation_ft", "workout_type"}
    sort = sort if sort in allowed else "date"
    direction = "ASC" if str(direction).lower() == "asc" else "DESC"

    where, params = _window(start, end, workout_type, status)
    rows = conn.execute(
        f"SELECT * FROM runs{where} ORDER BY {sort} IS NULL, {sort} {direction},"
        f" date DESC LIMIT ?", [*params, limit]
    ).fetchall()
    return [dict(r) for r in rows]


def get_run(conn, run_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def neighbours(conn, run: dict) -> tuple[int | None, int | None]:
    """Previous/next run by date, for arrow navigation on the detail page."""
    prev = conn.execute(
        "SELECT id FROM runs WHERE date < ? OR (date = ? AND id < ?)"
        " ORDER BY date DESC, id DESC LIMIT 1",
        (run["date"], run["date"], run["id"])).fetchone()
    nxt = conn.execute(
        "SELECT id FROM runs WHERE date > ? OR (date = ? AND id > ?)"
        " ORDER BY date ASC, id ASC LIMIT 1",
        (run["date"], run["date"], run["id"])).fetchone()
    return (prev["id"] if prev else None), (nxt["id"] if nxt else None)


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM runs GROUP BY status")}


def upcoming(conn: sqlite3.Connection, limit: int = 6) -> list[dict]:
    """The next runs on the plan, from today forward."""
    rows = conn.execute(
        "SELECT * FROM runs WHERE status = 'planned' AND date >= date('now')"
        " ORDER BY date LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def weekly_weight(conn: sqlite3.Connection) -> list[dict]:
    """Body weight, where the sheet recorded one."""
    rows = conn.execute(
        "SELECT week_start, weight_lb FROM weeks WHERE weight_lb IS NOT NULL"
        " ORDER BY week_start").fetchall()
    return [dict(r) for r in rows]


def plan_weeks(conn: sqlite3.Connection) -> list[dict]:
    """The spreadsheet's own weekly rows, totals and all."""
    rows = conn.execute("SELECT * FROM weeks ORDER BY week_start").fetchall()
    return [dict(r) for r in rows]


# --- Apple Health series ----------------------------------------------------

def training_block_start(conn: sqlite3.Connection) -> str | None:
    """Monday of the plan's first week.

    Importing Apple Health brings in years of running that predates this marathon
    block. The dashboard scopes to the block by default so "average pace" doesn't
    silently become a lifetime average.
    """
    row = conn.execute("SELECT MIN(week_start) AS start FROM weeks").fetchone()
    return row["start"] if row and row["start"] else None


def has_runs_before(conn: sqlite3.Connection, when: str) -> bool:
    """Is there running history older than the training block?"""
    return conn.execute(
        "SELECT 1 FROM runs WHERE status = 'completed' AND date < ? LIMIT 1",
        (when,)).fetchone() is not None


def hr_series(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT offset_sec, bpm FROM hr_samples WHERE run_id = ? ORDER BY offset_sec",
        (run_id,)).fetchall()
    return [dict(r) for r in rows]


def route(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT offset_sec, lat, lon, altitude_ft FROM route_points"
        " WHERE run_id = ? ORDER BY offset_sec", (run_id,)).fetchall()
    return [dict(r) for r in rows]


def _haversine_mi(lat1, lon1, lat2, lon2) -> float:
    from math import asin, cos, radians, sin, sqrt

    EARTH_MI = 3958.7613
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = (sin(dlat / 2) ** 2
         + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2)
    return 2 * EARTH_MI * asin(sqrt(a))


def bearing(a: dict, b: dict) -> float:
    """Compass heading travelled from one point to the next, in degrees."""
    from math import atan2, cos, degrees, radians, sin

    lat1, lat2 = radians(a["lat"]), radians(b["lat"])
    dlon = radians(b["lon"] - a["lon"])
    y = sin(dlon) * cos(lat2)
    x = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)
    return (degrees(atan2(y, x)) + 360) % 360


def _blend_angle(first: float, second: float, fraction: float) -> float:
    """Interpolate two compass bearings the short way round.

    Straight averaging sends 350 degrees and 10 degrees through south instead of
    through north, which would flip a tailwind into a headwind.
    """
    delta = ((second - first + 180) % 360) - 180
    return (first + delta * fraction) % 360


def weather_hours(conn: sqlite3.Connection, place: str, day: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM weather_hours WHERE place = ? AND hour_ts LIKE ?"
        " ORDER BY hour_ts", (place, f"{day}%")).fetchall()
    return [dict(r) for r in rows]


def weather_at(hours: list[dict], moment) -> dict | None:
    """Conditions at an instant, interpolated between the surrounding readings.

    The underlying data is hourly; this smooths the steps rather than pretending
    to a resolution the source does not have.
    """
    from datetime import datetime

    if not hours:
        return None
    target = moment.replace(tzinfo=None)
    stamps = [datetime.fromisoformat(h["hour_ts"]) for h in hours]
    if target <= stamps[0]:
        return dict(hours[0])
    if target >= stamps[-1]:
        return dict(hours[-1])

    for i in range(len(stamps) - 1):
        if stamps[i] <= target <= stamps[i + 1]:
            span = (stamps[i + 1] - stamps[i]).total_seconds()
            fraction = ((target - stamps[i]).total_seconds() / span) if span else 0
            before, after = hours[i], hours[i + 1]
            out = {}
            for key in ("temp_f", "apparent_f", "humidity_pct", "dew_point_f",
                        "wind_mph", "precip_in"):
                a, b = before.get(key), after.get(key)
                out[key] = (a + (b - a) * fraction) if (a is not None and b is not None) else a
            a, b = before.get("wind_dir_deg"), after.get("wind_dir_deg")
            out["wind_dir_deg"] = (_blend_angle(a, b, fraction)
                                   if (a is not None and b is not None) else a)
            return out
    return dict(hours[0])


def headwind(wind_mph: float | None, wind_from_deg: float | None,
             heading_deg: float | None) -> float | None:
    """Wind along the direction of travel: positive is a headwind, negative a tail.

    Meteorological wind direction is where the wind blows *from*, so wind coming
    from straight ahead is a full headwind.
    """
    from math import cos, radians

    if wind_mph is None or wind_from_deg is None or heading_deg is None:
        return None
    return wind_mph * cos(radians(wind_from_deg - heading_deg))


def splits(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    """Per-mile splits walked out of the GPS track.

    The single most useful thing the watch gives a marathon plan that the
    spreadsheet never could: whether a long run held pace or fell apart at 16.
    """
    points = route(conn, run_id)
    if len(points) < 2:
        return []

    # The GPS track is typically a percent or two short of the distance the watch
    # reports -- the watch fuses GPS with stride length, and sampling cuts corners.
    # Scale the track onto the run's stated distance so seventeen miles of splits
    # plus the remainder add up to the number at the top of the page.
    run = conn.execute(
        "SELECT distance_mi, duration_sec, started_at, date, weather_place"
        " FROM runs WHERE id = ?", (run_id,)).fetchone()
    stated = run["distance_mi"]

    from datetime import datetime, timedelta

    hours = (weather_hours(conn, run["weather_place"], run["date"])
             if run["weather_place"] else [])
    beats = conn.execute(
        "SELECT offset_sec, bpm FROM hr_samples WHERE run_id = ? ORDER BY offset_sec",
        (run_id,)).fetchall()
    beat_offsets = [b["offset_sec"] for b in beats]
    began = datetime.fromisoformat(run["started_at"]) if run["started_at"] else None
    raw_total = sum(
        _haversine_mi(a["lat"], a["lon"], b["lat"], b["lon"])
        for a, b in zip(points, points[1:]))
    scale = (stated / raw_total) if (stated and raw_total) else 1.0

    out: list[dict] = []
    covered = 0.0
    mile_start_offset = points[0]["offset_sec"]
    mile_start_elevation = points[0]["altitude_ft"]
    mile_start_point = points[0]

    for previous, current in zip(points, points[1:]):
        covered += _haversine_mi(previous["lat"], previous["lon"],
                                 current["lat"], current["lon"]) * scale
        if covered >= 1.0:
            elapsed = current["offset_sec"] - mile_start_offset
            climb = None
            if current["altitude_ft"] is not None and mile_start_elevation is not None:
                climb = round(current["altitude_ft"] - mile_start_elevation)
            split = {"mile": len(out) + 1, "seconds": elapsed,
                     "pace_sec_per_mi": elapsed / covered if covered else None,
                     "elevation_change_ft": climb,
                     "avg_hr": _average_bpm(beats, beat_offsets,
                                            mile_start_offset, current["offset_sec"])}
            _attach_weather(split, hours, began, current, mile_start_point)
            out.append(split)
            covered -= 1.0
            mile_start_offset = current["offset_sec"]
            mile_start_elevation = current["altitude_ft"]
            mile_start_point = current

    if covered > 0.05 and points[-1]["offset_sec"] > mile_start_offset:
        elapsed = points[-1]["offset_sec"] - mile_start_offset
        last_climb = None
        if points[-1]["altitude_ft"] is not None and mile_start_elevation is not None:
            last_climb = round(points[-1]["altitude_ft"] - mile_start_elevation)
        split = {"mile": None, "partial": round(covered, 2), "seconds": elapsed,
                 "pace_sec_per_mi": elapsed / covered,
                 "elevation_change_ft": last_climb,
                 "avg_hr": _average_bpm(beats, beat_offsets, mile_start_offset,
                                        points[-1]["offset_sec"])}
        _attach_weather(split, hours, began, points[-1], mile_start_point)
        out.append(split)

    # How each split sat against the run's overall pace -- the one thing a splits
    # table can show that its own numbers don't already say: whether the run
    # faded, held, or came home faster than it went out. Runs last, so the
    # part-mile finish is included; it used to be skipped and rendered as an
    # undefined value.
    average = (run["duration_sec"] / stated) if (stated and run["duration_sec"]) else None
    for split in out:
        split["deviation_sec"] = ((split["pace_sec_per_mi"] - average)
                                  if (average and split.get("pace_sec_per_mi"))
                                  else None)
    return out


def track_for_map(points: list[dict], max_points: int = 2000) -> list[list[float]]:
    """[[lat, lon], ...] thinned to something a map can draw smoothly.

    A long run is 14k GPS points; at any zoom a map shows the same line from
    2k. Splits still use the full-resolution track -- this is display only.
    """
    if not points:
        return []
    if len(points) <= max_points:
        return [[p["lat"], p["lon"]] for p in points]
    stride = len(points) / max_points
    kept = [points[int(i * stride)] for i in range(max_points)]
    kept[-1] = points[-1]          # never lose the finish
    return [[p["lat"], p["lon"]] for p in kept]


def _average_bpm(beats, offsets, start: int, end: int, minimum: int = 3) -> int | None:
    """Mean heart rate over one split's slice of time.

    Needs a few readings to mean anything -- Apple keeps only a handful of
    background samples for older workouts, and one stray beat should not stand in
    for a whole mile.
    """
    import bisect

    low = bisect.bisect_left(offsets, start)
    high = bisect.bisect_right(offsets, end)
    window = beats[low:high]
    if len(window) < minimum:
        return None
    return round(sum(b["bpm"] for b in window) / len(window))


def elevation_scale(splits_out: list[dict], floor: float = 15.0) -> float:
    """Feet of climb that should tilt the slope glyph to its steepest.

    Scaled per run so a rolling course and a flat one both read, rather than
    every mile of a flat run drawing as a dead-level line.
    """
    climbs = [abs(s["elevation_change_ft"]) for s in splits_out
              if s.get("elevation_change_ft") is not None]
    return max(floor, max(climbs)) if climbs else floor


def _attach_weather(split: dict, hours, began, end_point, start_point) -> None:
    """Conditions at the moment this split ended, and the wind along its heading."""
    from datetime import timedelta

    if not hours or began is None:
        return
    moment = began + timedelta(seconds=end_point["offset_sec"])
    conditions = weather_at(hours, moment)
    if not conditions:
        return
    heading = bearing(start_point, end_point)
    split.update(
        temp_f=conditions.get("temp_f"),
        apparent_f=conditions.get("apparent_f"),
        humidity_pct=conditions.get("humidity_pct"),
        wind_mph=conditions.get("wind_mph"),
        wind_dir_deg=conditions.get("wind_dir_deg"),
        heading_deg=heading,
        headwind_mph=headwind(conditions.get("wind_mph"),
                              conditions.get("wind_dir_deg"), heading),
    )


def route_svg_path(points: list[dict], width: int = 520, height: int = 300,
                   pad: int = 12) -> dict | None:
    """Project a track into an SVG polyline. Kept as the no-tiles fallback."""
    if len(points) < 2:
        return None
    lats = [p["lat"] for p in points]
    lons = [p["lon"] for p in points]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)

    # Longitude degrees shrink with latitude; without this correction a
    # north-south route looks stretched.
    from math import cos, radians

    scale_lon = cos(radians((min_lat + max_lat) / 2)) or 1.0
    span_x = (max_lon - min_lon) * scale_lon or 1e-9
    span_y = (max_lat - min_lat) or 1e-9
    scale = min((width - 2 * pad) / span_x, (height - 2 * pad) / span_y)

    offset_x = (width - span_x * scale) / 2
    offset_y = (height - span_y * scale) / 2

    coords = []
    for point in points:
        x = offset_x + (point["lon"] - min_lon) * scale_lon * scale
        # SVG y grows downward; latitude grows north.
        y = offset_y + (max_lat - point["lat"]) * scale
        coords.append(f"{x:.1f},{y:.1f}")
    return {"points": " ".join(coords), "width": width, "height": height,
            "start": coords[0], "end": coords[-1]}


def deviation_scale(splits_out: list[dict], floor: float = 20.0) -> float:
    """Seconds that should fill half the deviation bar.

    Set from the typical mile, not the worst one. A run with two miles spent
    waiting at a level crossing has a maximum deviation ten times the median, and
    scaling to it flattens every real difference to a pixel or two. Three times
    the median gap keeps ordinary miles legible; the rare mile past that clamps
    at the edge and is drawn with a flat end to show it ran off the scale. Its
    number is printed either way, so nothing is hidden.
    """
    gaps = sorted(abs(s["deviation_sec"]) for s in splits_out
                  if s.get("deviation_sec") is not None)
    if not gaps:
        return floor
    middle = len(gaps) // 2
    median = gaps[middle] if len(gaps) % 2 else (gaps[middle - 1] + gaps[middle]) / 2
    return max(floor, median * 3)
