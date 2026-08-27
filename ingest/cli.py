"""Load spreadsheet exports into the runs database.

    python -m ingest.cli --inspect data/2026-marathon.csv     # dry run
    python -m ingest.cli --db data/runs.db data/*.csv

Two sheet shapes are recognized, picked automatically per file:

  * a week-per-row training plan with a column per weekday
  * a tidy log with one run per row
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import normalize as nz
from .apple_health import SOURCE as HEALTH_SOURCE
from .apple_health import load_export
from .csv_loader import load_csv
from .exclude import excluded_keys
from .csv_loader import SOURCE as CSV_SOURCE
from .merge import (assign_week, find_match, merge_workout, plan_candidates,
                    week_bounds)
from .schema import FIELDS, connect, init_db, load_overrides
from .weather import backfill as backfill_weather
from .weekly_plan_loader import SOURCE as PLAN_SOURCE
from .weekly_plan_loader import load_weekly_plan, looks_like_weekly_plan

RUN_COLUMNS = ["run_key", "source", "date", "status", "week_number", "distance_mi",
               "duration_sec", "pace_sec_per_mi", "avg_hr", "max_hr", "elevation_ft",
               "cadence", "calories", "workout_type", "effort", "route", "weather",
               "shoes", "notes", "extra", "imported_at"]

# Measurements. Once Apple Health has supplied these, the spreadsheet's
# hand-typed figures may never overwrite them again -- the watch's 3:22:19 beats
# a rounded 3:22:24 every time.
MEASURED_FIELDS = ["status", "distance_mi", "duration_sec", "pace_sec_per_mi"]

# Also measured, but a plain spreadsheet may legitimately carry them, so they
# stay in RUN_COLUMNS. They must still survive a sheet re-import that has nothing
# to say about them -- otherwise a blank cell erases the watch's reading.
SENSOR_FIELDS = ["avg_hr", "max_hr", "elevation_ft", "cadence", "calories",
                 # The run moved to the day the watch says it happened; a sheet
                 # re-import must not drag it back to the planned weekday.
                 "date"]

# Fields a run logged in the app owns. On re-import the sheet takes them back
# only when it actually has a time for that run -- see run_upsert().
LOGGED_FIELDS = [*MEASURED_FIELDS, "notes"]

WEEK_COLUMNS = ["week_key", "source", "week_number", "week_start", "sheet_miles",
                "sheet_seconds", "weight_lb", "notes", "imported_at"]


def upsert(table: str, columns: list[str], key: str) -> str:
    updatable = [c for c in columns if c not in (key, "source")]
    return (f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({', '.join(':' + c for c in columns)}) "
            f"ON CONFLICT({key}) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in updatable))


def run_upsert() -> str:
    """Upsert runs, protecting times entered in the app.

    The sheet is the record of truth wherever it has one: an incoming row with a
    real time (status 'completed') takes the run back and clears the app flag.
    Where the sheet is still blank or zero, whatever was logged in the app stays.
    """
    plain = [c for c in RUN_COLUMNS
             if c not in ("run_key", "source", "logged_at", *LOGGED_FIELDS)]
    sheet_has_time = "excluded.status = 'completed'"
    measured = "runs.measured_source = 'apple-health'"
    assignments = [
        f"{c}=CASE WHEN {measured} THEN runs.{c} ELSE excluded.{c} END"
        if c in SENSOR_FIELDS else f"{c}=excluded.{c}"
        for c in plain
    ]
    for column in LOGGED_FIELDS:
        # Health outranks the app, which outranks the sheet. `notes` is the
        # workout name -- intent, not measurement -- so the sheet keeps it.
        guard = (f"WHEN {measured} THEN runs.{column} " if column in MEASURED_FIELDS else "")
        assignments.append(
            f"{column}=CASE {guard}"
            f"WHEN runs.logged_at IS NULL OR {sheet_has_time}"
            f" THEN excluded.{column} ELSE runs.{column} END")
    # A run the sheet has now recorded is no longer an app entry.
    assignments.append(
        f"logged_at=CASE WHEN {measured} THEN runs.logged_at"
        f" WHEN {sheet_has_time} THEN NULL ELSE runs.logged_at END")
    assignments.append(
        f"prior_distance_mi=CASE WHEN {measured} THEN runs.prior_distance_mi"
        f" WHEN {sheet_has_time} THEN NULL ELSE runs.prior_distance_mi END")
    return (f"INSERT INTO runs ({', '.join(RUN_COLUMNS)}) "
            f"VALUES ({', '.join(':' + c for c in RUN_COLUMNS)}) "
            f"ON CONFLICT(run_key) DO UPDATE SET " + ", ".join(assignments))


def logged_conflicts(conn, rows: list[dict]) -> tuple[list[str], int]:
    """(runs whose app-entered time the sheet is about to take back, runs kept)."""
    existing = {
        r["run_key"]: r for r in conn.execute(
            "SELECT run_key, date, duration_sec FROM runs WHERE logged_at IS NOT NULL")
    }
    if not existing:
        return [], 0
    taken_back, kept = [], 0
    for row in rows:
        prior = existing.get(row["run_key"])
        if prior is None:
            continue
        if row.get("status") == "completed":
            was, now = prior["duration_sec"], row.get("duration_sec")
            if was != now:
                taken_back.append(
                    f"{prior['date']}: logged {nz.format_duration(was)} in the app, "
                    f"sheet says {nz.format_duration(now)} -- keeping the sheet's")
            else:
                taken_back.append(f"{prior['date']}: sheet now matches what you logged")
        else:
            kept += 1
    return taken_back, kept


def detect_source(path: Path) -> str:
    """Which loader this file needs. Health exports are zips; the rest are CSV."""
    import zipfile

    if zipfile.is_zipfile(path):
        return "health"
    with path.open("rb") as fh:
        if b"HealthData" in fh.read(4096):
            return "health"
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for i, row in enumerate(csv.reader(fh)):
            if i >= 10:
                break
            if looks_like_weekly_plan(row):
                return "plan"
    return "tidy"


def report_health(path: Path, result, conn=None) -> None:
    print(f"\n{path.name}  (Apple Health export)")
    workouts = result.workouts
    print(f"  running workouts: {result.seen} found, {len(workouts)} after de-duplication")
    if workouts:
        dates = sorted(w["date"] for w in workouts)
        print(f"  date range: {dates[0]} to {dates[-1]}")
        print(f"  with a GPS route: {result.routes_found}"
              f" · heart-rate samples: {result.hr_series_found}")
    if result.duplicates:
        print("  same run recorded twice:")
        for line in result.duplicates:
            print(f"    {line}")
    for warning in result.warnings:
        print(f"  ! {warning}")

    if conn is None or not workouts:
        return

    # Mirror the write order so the report agrees with what will happen: the
    # Same week-at-a-time assignment the import will do, so the dry run and the
    # real thing can't disagree.
    matched = shifted = 0
    new: list[dict] = []
    notes: list[str] = []
    for monday, week in group_by_week(workouts).items():
        rows = plan_candidates(conn, monday, week_bounds(monday)[1])
        for workout, row, note in assign_week(week, rows):
            if row is None:
                new.append(workout)
            elif note:
                shifted += 1
                notes.append(note)
            else:
                matched += 1

    print(f"\n  {matched} line up with a run already in your plan; "
          f"{shifted} were run early or late but match a plan day; "
          f"{len(new)} would come in as new runs")
    if notes:
        print("  runs that did not happen on the day the plan put them:")
        for note in notes:
            print(f"    {note}")
    if new:
        dates = sorted(w["date"] for w in new)
        print(f"    new runs span {dates[0]} to {dates[-1]}")

    reconcile_weeks(conn, workouts)


def reconcile_weeks(conn, workouts: list[dict]) -> None:
    """Health mileage against the spreadsheet's own weekly totals.

    Small gaps are expected -- GPS measures 4.03 where the plan says "4 Miles".
    A large one means matching went wrong.
    """
    weeks = conn.execute(
        "SELECT week_number, week_start, sheet_miles FROM weeks"
        " WHERE sheet_miles IS NOT NULL ORDER BY week_start").fetchall()
    if not weeks:
        return
    from datetime import date, timedelta

    lines = []
    for week in weeks:
        start = date.fromisoformat(week["week_start"])
        end = start + timedelta(days=6)
        measured = sum(w["distance_mi"] or 0 for w in workouts
                       if start <= date.fromisoformat(w["date"]) <= end)
        if not measured:
            continue
        gap = measured - week["sheet_miles"]
        if abs(gap) > 1.0:
            lines.append(f"week {week['week_number']}: Health {measured:.2f} mi vs "
                         f"sheet {week['sheet_miles']} mi ({gap:+.2f})")
    if lines:
        print("\n  weeks where Health and the sheet disagree by more than a mile:")
        for line in lines:
            print(f"    {line}")
    else:
        print("  Health mileage agrees with every overlapping week in the sheet")


def report_plan(path: Path, result) -> None:
    print(f"\n{path.name}  (week-per-row training plan)")
    print(f"  header row: {result.header_row + 1}")
    print("  run slots (each Time column covers the days before it):")
    for days, time_col in result.blocks:
        names = " / ".join(name for name, _ in days)
        print(f"    {names:<24} time in column {time_col + 1}")
    if result.trailing_days:
        names = " / ".join(name for name, _ in result.trailing_days)
        print(f"    {names:<24} no Time column (rest, or a workout that slipped a day)")
    print(f"  weeks: {result.rows_read}")
    print(f"  runs:  {result.completed} completed, {result.skipped} skipped "
          f"(on the plan, time recorded as 0), {result.planned} still ahead")
    if result.derived_distances:
        print("  distances worked out from the weekly total:")
        for line in result.derived_distances:
            print(f"    {line}")
    if result.discrepancies:
        print("  weeks where the day cells don't sum to the sheet's own total:")
        for line in result.discrepancies:
            print(f"    {line}")
    for warning in result.warnings:
        print(f"  ! {warning}")


def report_tidy(path: Path, result) -> None:
    print(f"\n{path.name}  (one run per row)")
    print(f"  header row: {result.header_row + 1}")
    print("  detected columns:")
    for field in FIELDS:
        if field in ("status", "week_number"):
            continue
        header = result.mapping.get(field)
        print(f"    {field:<16} <- {header if header else '(not found)'}")
    if result.unmapped:
        print(f"  carried along as extra: {', '.join(result.unmapped)}")
    print(f"  rows: {result.rows_read} read, {len(result.rows)} usable, "
          f"{result.skipped_no_date} without a date, "
          f"{result.skipped_no_activity} with no distance or duration")
    for warning in result.warnings:
        print(f"  ! {warning}")


def preview(result, limit: int = 6) -> None:
    completed = [r for r in result.rows if r.get("status", "completed") == "completed"]
    if not completed:
        return
    print(f"\n  first {min(limit, len(completed))} completed runs:")
    print(f"    {'date':<12} {'mi':>6} {'time':>8} {'pace':>7}  {'type':<14} workout")
    for row in completed[:limit]:
        print(f"    {row['date']:<12} "
              f"{(row.get('distance_mi') or 0):>6.2f} "
              f"{nz.format_duration(row.get('duration_sec')):>8} "
              f"{nz.format_pace(row.get('pace_sec_per_mi')):>7}  "
              f"{(row.get('workout_type') or ''):<14} "
              f"{(row.get('notes') or '')[:32]}")


def weather_targets(conn) -> list[dict]:
    """Runs in the current training block that have a track to place them.

    Scoped to the block on purpose: weather is for understanding the marathon
    build, and seven years of history would be a lot of lookups for runs nobody
    is going to re-examine.
    """
    block = conn.execute("SELECT MIN(week_start) AS s FROM weeks").fetchone()["s"]
    if not block:
        return []
    rows = conn.execute("""
        SELECT r.id, r.date, AVG(p.lat) AS lat, AVG(p.lon) AS lon
        FROM runs r JOIN route_points p ON p.run_id = r.id
        WHERE r.status = 'completed' AND r.date >= ?
        GROUP BY r.id ORDER BY r.date
    """, (block,)).fetchall()
    return [dict(r) for r in rows]


def run_weather(conn, args) -> None:
    """Fill in hourly weather. Never fatal -- an import must survive a bad network."""
    targets = weather_targets(conn)
    if not targets:
        return
    print(f"\nWeather: {len(targets)} runs in the training block have a track")
    result = backfill_weather(conn, targets)
    print(f"  {result['fetched']} day(s) fetched, {result['cached']} already cached, "
          f"{result['hours']} hourly readings stored")
    if result["failures"]:
        print(f"  ! {len(result['failures'])} lookup(s) failed -- everything else "
              f"imported fine, re-run `make ingest` to retry:")
        for line in result["failures"][:5]:
            print(f"      {line}")


def group_by_week(workouts: list[dict]) -> dict[str, list[dict]]:
    """Measured runs bucketed by the Monday of their week, oldest first.

    Matching happens a week at a time: within a week the plan's rows and the
    week's actual runs are the same set of workouts, just possibly on different
    days.
    """
    weeks: dict[str, list[dict]] = {}
    for workout in sorted(workouts, key=lambda w: (w["date"], -(w["duration_sec"] or 0))):
        weeks.setdefault(week_bounds(workout["date"])[0], []).append(workout)
    return weeks


def count(result) -> int:
    return len(getattr(result, "workouts", None) or getattr(result, "rows", []))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load run data into SQLite.")
    parser.add_argument("files", nargs="+",
                        help="CSV export(s) of your sheet, or an Apple Health export.zip")
    parser.add_argument("--db", default="data/runs.db")
    parser.add_argument("--inspect", action="store_true",
                        help="Show what would be imported; write nothing.")
    parser.add_argument("--config", default="config/columns.json",
                        help="Optional canonical-field -> header overrides (tidy sheets only).")
    parser.add_argument("--replace", action="store_true",
                        help="Delete existing rows from this source before loading.")
    parser.add_argument("--no-routes", action="store_true",
                        help="Skip GPS tracks from a Health export (much smaller database).")
    parser.add_argument("--no-weather", action="store_true",
                        help="Skip the weather lookup, keeping the import fully offline.")
    args = parser.parse_args(argv)

    overrides = load_overrides(args.config)
    if overrides:
        print(f"using column overrides from {args.config}: {overrides}")

    conn = connect(args.db)
    init_db(conn)
    try:
        # Sheets before Health exports, always. A measurement merges into the
        # plan row for its day, so those rows have to exist first -- otherwise
        # the same run lands twice, once from each source.
        paths = [Path(name) for name in args.files]
        missing = [p for p in paths if not p.exists()]
        for path in missing:
            print(f"skipping {path}: not found", file=sys.stderr)
        found = [p for p in paths if p.exists()]
        shapes = {p: detect_source(p) for p in found}
        ordered = sorted(found, key=lambda p: shapes[p] == "health")

        results = []
        for path in ordered:
            shape = shapes[path]
            if shape == "health":
                result = load_export(path, with_routes=not args.no_routes)
                report_health(path, result, conn)
            elif shape == "plan":
                result = load_weekly_plan(path)
                report_plan(path, result)
                preview(result)
            else:
                result = load_csv(path, overrides=overrides)
                report_tidy(path, result)
                preview(result)
            results.append((path, shape, result))

        if not results:
            print("\nNothing to import.", file=sys.stderr)
            return 1

        total = sum(count(r) for _, _, r in results)
        if args.inspect:
            print(f"\nDry run: {total} runs would be imported into {args.db}.")
            print("Looks right? Run `make ingest`.")
            return 0

        if total == 0:
            print("\nNo usable rows found -- nothing written.", file=sys.stderr)
            return 1

        if args.replace:
            for source in (PLAN_SOURCE, CSV_SOURCE, HEALTH_SOURCE):
                removed = conn.execute("DELETE FROM runs WHERE source = ?",
                                       (source,)).rowcount
                if removed:
                    print(f"\nremoved {removed} existing {source} rows")

        imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        weeks_written = 0
        for path, shape, result in results:
            if shape == "health":
                weeks_written += write_health(conn, result, args)
            else:
                runs = [{c: row.get(c) for c in RUN_COLUMNS} for row in result.rows]
                banned_keys, _ = excluded_keys(conn)
                skipped = [r for r in runs if r["run_key"] in banned_keys]
                runs = [r for r in runs if r["run_key"] not in banned_keys]
                if skipped:
                    print(f"\nskipping {len(skipped)} row(s) you excluded")
                taken_back, kept = logged_conflicts(conn, runs)
                if kept:
                    print(f"\nkept {kept} time(s) you logged in the app "
                          f"(the sheet still has no time for them)")
                if taken_back:
                    print("\nthe sheet has caught up on runs you logged in the app:")
                    for line in taken_back:
                        print(f"  {line}")
                conn.executemany(run_upsert(), runs)
                if getattr(result, "weeks", None):
                    weeks = [{c: w.get(c) for c in WEEK_COLUMNS} for w in result.weeks]
                    conn.executemany(upsert("weeks", WEEK_COLUMNS, "week_key"), weeks)
                    weeks_written += len(weeks)
            conn.execute(
                "INSERT INTO import_log (source, filename, rows_read, rows_kept,"
                " imported_at, mapping) VALUES (?, ?, ?, ?, ?, ?)",
                (HEALTH_SOURCE if shape == "health"
                 else (PLAN_SOURCE if shape == "plan" else CSV_SOURCE),
                 path.name, getattr(result, "rows_read", 0) or getattr(result, "seen", 0),
                 count(result), imported_at, shape),
            )
        conn.commit()

        if not args.no_weather:
            try:
                run_weather(conn, args)
            except Exception as problem:      # noqa: BLE001 - weather is optional
                print(f"\nWeather lookup skipped: {problem}", file=sys.stderr)

        counts = conn.execute(
            "SELECT status, COUNT(*) AS n FROM runs GROUP BY status").fetchall()
        samples = conn.execute(
            "SELECT (SELECT COUNT(*) FROM hr_samples), (SELECT COUNT(*) FROM route_points)"
        ).fetchone()
    finally:
        conn.close()

    tally = ", ".join(f"{r[1]} {r[0]}" for r in counts)
    print(f"\nImported {total} runs" + (f" and {weeks_written} weeks" if weeks_written else ""))
    print(f"{args.db} now holds {tally}.")
    if samples[0] or samples[1]:
        print(f"plus {samples[0]:,} heart-rate samples and {samples[1]:,} route points.")
    print("Start the app with `make start` -> http://localhost:8000")
    return 0


def write_health(conn, result, args) -> int:
    """Merge measured workouts into the plan.

    Three passes, in order of confidence, because a later pass must never claim
    a plan row a more certain match still needs:

      1. same calendar day  -- the longest run that day claims the plan row, so a
         two-a-day's second outing correctly gets a row of its own
      2. same training week, corroborating distance -- the run happened, just not
         on the day the plan put it
      3. whatever is left is a genuinely new run
    """
    tally = {"merged": 0, "shifted": 0, "inserted": 0, "updated": 0}
    notes: list[str] = []

    _, banned = excluded_keys(conn)
    kept = [w for w in result.workouts if w["health_id"] not in banned]
    dropped = len(result.workouts) - len(kept)
    if dropped:
        print(f"\n  skipping {dropped} workout(s) you excluded")

    for monday, workouts in group_by_week(kept).items():
        sunday = week_bounds(monday)[1]
        rows = plan_candidates(conn, monday, sunday)
        for workout, row, note in assign_week(workouts, rows):
            # Routes were already dropped at parse time by --no-routes; heart
            # rate is always worth keeping.
            _, outcome = merge_workout(conn, workout, store_samples=True,
                                       row=row, note=note)
            tally[outcome] += 1
            if note:
                notes.append(note)

    print(f"\nApple Health: {tally['merged']} merged into the run the plan had "
          f"that day, {tally['shifted']} matched to a plan day they were run early "
          f"or late, {tally['inserted']} added as new runs, "
          f"{tally['updated']} already known")
    if notes:
        print("\n  runs that did not happen on the day the plan put them:")
        for note in notes:
            print(f"    {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
