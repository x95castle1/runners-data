"""Folding a measured workout into the run it belongs to.

The training plan supplies intent -- what the workout was, which week it belongs
to, how far it was meant to be. Apple Health supplies measurement. They describe
the same run, so they share one row: the plan row is the spine and the Health
data fills in what actually happened.

Both the export.zip backfill and the REST push land here, so the two paths can
never drift apart.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

# What Health measured. These overwrite whatever the sheet or the app had.
MEASURED = [
    "started_at", "duration_sec", "distance_mi", "pace_sec_per_mi",
    "avg_hr", "max_hr", "min_hr", "elevation_ft", "calories", "cadence",
    "temperature_f", "humidity_pct", "source_name", "health_id",
    "measured_source", "route_points",
]

# What the plan owns. A merge never touches these.
INTENT = ["workout_type", "week_number", "notes", "run_key", "source"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Cost floor for a same-day pairing whose distances disagree. Any genuine
# distance match scores far below this, so those are always settled first.
SAME_DAY_FALLBACK = 100.0


def week_bounds(iso_date: str) -> tuple[str, str]:
    """Monday-to-Sunday around a date, matching how the plan counts a week."""
    from datetime import date, timedelta

    day = date.fromisoformat(iso_date)
    monday = day - timedelta(days=day.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def _day_gap(a: str, b: str) -> int:
    from datetime import date

    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def pair_cost(workout: dict, row: dict, max_days: int = 6) -> float | None:
    """How well a measured run fits a plan row, or None if it plainly doesn't.

    Distance dominates and the day only breaks ties, because within a training
    week the distance identifies a run and the weekday does not: the plan pins
    every workout to a fixed day, and real weeks slide. Matching on the day alone
    puts a 14-mile run into the slot that said "6 Miles" and leaves the actual
    14-mile row to be counted a second time.
    """
    gap = _day_gap(workout["date"], row["date"])
    if gap > max_days:
        return None

    planned = row.get("distance_mi")
    measured = workout.get("distance_mi")
    if planned is None or measured is None:
        # Nothing to corroborate with, so only an exact day is convincing.
        return float(gap) if gap == 0 else None

    difference = abs(measured - planned)
    if difference <= max(0.75, planned * 0.2):
        # Corroborating distance: the strongest evidence, day only breaks ties.
        return difference + 0.05 * gap
    if gap == 0:
        # Same day but a distance the plan didn't call for -- they ran something
        # other than what was written. Weak evidence, but real: ranked far below
        # every distance match so it only ever claims a leftover row.
        return SAME_DAY_FALLBACK + difference
    return None


def assign_week(workouts: list[dict], rows: list[dict],
                max_days: int = 6) -> list[tuple[dict, dict | None, str]]:
    """Pair each measured run in a week with the plan row it belongs to.

    Greedy best-fit: the most convincing pair is settled first, then the next,
    so a run can't steal a row that fits another run better. Pure function --
    the database work happens in the caller.
    """
    pairs = []
    for workout in workouts:
        for row in rows:
            cost = pair_cost(workout, row, max_days)
            if cost is not None:
                pairs.append((cost, workout, row))
    pairs.sort(key=lambda item: (item[0], item[1]["date"]))

    taken_workouts: set[str] = set()
    taken_rows: set[int] = set()
    assigned: dict[str, tuple[dict, str]] = {}
    for _, workout, row in pairs:
        if workout["health_id"] in taken_workouts or row["id"] in taken_rows:
            continue
        taken_workouts.add(workout["health_id"])
        taken_rows.add(row["id"])
        note = ""
        if row["date"] != workout["date"]:
            gap = _day_gap(workout["date"], row["date"])
            direction = "earlier" if row["date"] > workout["date"] else "later"
            note = (f"{workout['date']}: {workout['distance_mi']:.2f} mi matched to "
                    f"the plan's {row['date']} ({row['distance_mi']:.2f} mi) — ran "
                    f"{gap} day{'s' if gap > 1 else ''} {direction}")
        assigned[workout["health_id"]] = (row, note)

    return [(w, *assigned.get(w["health_id"], (None, ""))) for w in workouts]


def plan_candidates(conn: sqlite3.Connection, monday: str, sunday: str) -> list[dict]:
    """Plan rows in a week that no measured run has claimed yet."""
    rows = conn.execute(
        "SELECT * FROM runs WHERE source != 'apple-health' AND health_id IS NULL"
        "   AND date BETWEEN ? AND ? ORDER BY date, id",
        (monday, sunday)).fetchall()
    return [dict(r) for r in rows]


def find_match(conn: sqlite3.Connection, workout: dict) -> dict | None:
    """The single best plan row for one workout, ignoring competition.

    Used by the dry run's per-run reporting; the real import assigns a whole week
    at a time via assign_week().
    """
    monday, sunday = week_bounds(workout["date"])
    candidates = plan_candidates(conn, monday, sunday)
    scored = [(pair_cost(workout, row), row) for row in candidates]
    scored = [(c, r) for c, r in scored if c is not None]
    return min(scored, key=lambda item: item[0])[1] if scored else None


def merge_workout(conn: sqlite3.Connection, workout: dict, *,
                  store_samples: bool = True, row: dict | None = None,
                  note: str = "") -> tuple[int, str]:
    """Write one measured workout into `row`, or as a new run when row is None.

    Idempotent on health_id, so re-importing the same export changes nothing.
    """
    existing = conn.execute(
        "SELECT * FROM runs WHERE health_id = ?", (workout["health_id"],)
    ).fetchone()

    if existing is not None:
        run_id, outcome = existing["id"], "updated"
        _apply(conn, run_id, workout, keep_plan_distance=False)
    elif row is not None:
        run_id = row["id"]
        outcome = "shifted" if note else "merged"
        _apply(conn, run_id, workout, keep_plan_distance=True)
    else:
        run_id, outcome = _insert(conn, workout), "inserted"

    if store_samples:
        _store_samples(conn, run_id, workout)
    conn.commit()
    return run_id, outcome


def _apply(conn, run_id: int, workout: dict, *, keep_plan_distance: bool) -> None:
    """Overwrite the measured fields on an existing row, leaving intent alone."""
    if keep_plan_distance:
        # Remember what the plan called for before the watch's figure replaces it.
        conn.execute(
            "UPDATE runs SET prior_distance_mi = COALESCE(prior_distance_mi, distance_mi)"
            " WHERE id = ?", (run_id,))

    columns = [c for c in MEASURED if c in workout]
    assignments = ", ".join(f"{c} = :{c}" for c in columns)
    params = {c: workout[c] for c in columns}
    params["id"] = run_id
    params["date"] = workout["date"]
    # The row moves to the day the run actually happened -- the plan's weekday was
    # an intention, the watch's timestamp is what occurred.
    conn.execute(
        f"UPDATE runs SET {assignments}, date = :date, status = 'completed',"
        " logged_at = NULL WHERE id = :id", params)


def _insert(conn, workout: dict) -> int:
    """A run the plan never knew about: an unplanned outing, or pre-plan history."""
    columns = [c for c in MEASURED if c in workout]
    values = {c: workout[c] for c in columns}
    values.update(
        run_key=f"apple-health:{workout['health_id']}",
        source="apple-health",
        date=workout["date"],
        status="completed",
        workout_type=workout.get("workout_type") or "Run",
        notes=workout.get("notes"),
        imported_at=_now(),
    )
    names = list(values)
    cursor = conn.execute(
        f"INSERT INTO runs ({', '.join(names)})"
        f" VALUES ({', '.join(':' + n for n in names)})", values)
    return cursor.lastrowid


def _store_samples(conn, run_id: int, workout: dict) -> None:
    """Replace, never append -- re-importing must not double the series."""
    conn.execute("DELETE FROM hr_samples WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM route_points WHERE run_id = ?", (run_id,))
    if workout.get("hr_samples"):
        conn.executemany(
            "INSERT INTO hr_samples (run_id, offset_sec, bpm) VALUES (?, ?, ?)",
            [(run_id, offset, bpm) for offset, bpm in workout["hr_samples"]])
    if workout.get("route"):
        conn.executemany(
            "INSERT INTO route_points (run_id, offset_sec, lat, lon, altitude_ft,"
            " speed_mph) VALUES (?, ?, ?, ?, ?, ?)",
            [(run_id, *point) for point in workout["route"]])
        conn.execute("UPDATE runs SET route_points = ? WHERE id = ?",
                     (len(workout["route"]), run_id))
