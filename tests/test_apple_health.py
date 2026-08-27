"""Parsing an Apple Health export, and folding it into the training plan.

The fixture in tests/fixtures/build_health_export.py stands in for a real export
and carries the things that actually go wrong: two XML generations, metric and
imperial units, one run recorded by two apps, a two-a-day, a late-night run that
would slide a day under UTC, and heart-rate records outside every workout.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from build_health_export import build  # noqa: E402

from ingest import normalize as nz  # noqa: E402
from ingest.apple_health import (  # noqa: E402
    deduplicate, load_export, overlap_ratio, parse_gpx, split_quantity,
)
from ingest.cli import RUN_COLUMNS, run_upsert  # noqa: E402
from ingest.merge import (  # noqa: E402
    assign_week, merge_workout, pair_cost, plan_candidates, week_bounds,
)
from ingest.schema import connect, init_db  # noqa: E402


@pytest.fixture(scope="module")
def export_zip(tmp_path_factory):
    return build(tmp_path_factory.mktemp("health") / "export.zip")


@pytest.fixture(scope="module")
def result(export_zip):
    return load_export(export_zip)


def one(result, health_id):
    return next(w for w in result.workouts if w["health_id"] == health_id)


# --- units ------------------------------------------------------------------

@pytest.mark.parametrize("value,unit,to,expected", [
    ("10", "km", "mi", 6.2137),
    ("6.2", "mi", "mi", 6.2),
    ("4572", "cm", "ft", 150.0),
    ("150", "m", "ft", 492.126),
    ("18", "degC", "degF", 64.4),
    ("65", "degF", "degF", 65.0),
])
def test_quantities_follow_their_declared_unit(value, unit, to, expected):
    assert nz.parse_hk_quantity(value, unit, to=to) == pytest.approx(expected, abs=0.01)


def test_metadata_values_carry_number_and_unit_together():
    assert split_quantity("4572 cm") == ("4572", "cm")
    assert split_quantity("72.5 degF") == ("72.5", "degF")
    assert split_quantity("63") == ("63", "")


# --- timestamps -------------------------------------------------------------

def test_local_date_is_not_shifted_by_the_utc_offset(result):
    # 23:40 Central is the next day in UTC; the run belongs to the day it was run.
    assert one(result, "UUID-LATE")["date"] == "2026-08-18"


def test_started_at_keeps_the_time_of_day(result):
    assert one(result, "UUID-EASY")["started_at"].startswith("2026-08-24T06:15")


# --- both XML generations ---------------------------------------------------

def test_modern_export_reads_workout_statistics(result):
    long_run = one(result, "UUID-LONG")
    assert long_run["distance_mi"] == pytest.approx(18.02)
    assert long_run["duration_sec"] == 235 * 60
    assert (long_run["min_hr"], long_run["avg_hr"], long_run["max_hr"]) == (118, 149, 171)


def test_legacy_export_reads_element_attributes_in_metric(result):
    old = one(result, "UUID-OLD")
    assert old["distance_mi"] == pytest.approx(5.0, abs=0.01)   # 8.05 km
    assert old["duration_sec"] == 47 * 60


def test_metadata_weather_and_elevation(result):
    long_run = one(result, "UUID-LONG")
    assert long_run["temperature_f"] == pytest.approx(72.5)
    assert long_run["humidity_pct"] == pytest.approx(63)   # "6300 %" is 63%
    assert long_run["elevation_ft"] == pytest.approx(150.0, abs=0.1)


def test_pace_is_derived(result):
    easy = one(result, "UUID-EASY")
    assert easy["pace_sec_per_mi"] == pytest.approx(51 * 60 / 4.01, abs=0.1)


# --- de-duplication ---------------------------------------------------------

def test_the_same_run_from_two_apps_is_collapsed(result):
    assert result.seen == 7
    assert len(result.workouts) == 6
    assert not any(w["health_id"] == "UUID-LONG-NRC" for w in result.workouts)
    assert result.duplicates and "Apple Watch" in result.duplicates[0]


def test_the_richer_recording_wins():
    base = dict(started=None, ended=None, duration_sec=3600, avg_hr=None,
                route_file=None, distance_mi=5.0, hr_samples=[], route=[],
                source_name="x")
    from datetime import datetime, timedelta
    start = datetime(2026, 8, 22, 6, 0)
    poor = {**base, "started": start, "ended": start + timedelta(hours=1),
            "source_name": "Nike Run Club"}
    rich = {**base, "started": start + timedelta(minutes=1),
            "ended": start + timedelta(hours=1), "route_file": "r.gpx",
            "avg_hr": 150, "source_name": "Apple Watch"}
    kept, notes = deduplicate([poor, rich])
    assert len(kept) == 1
    assert kept[0]["source_name"] == "Apple Watch"
    assert notes


def test_a_two_a_day_is_not_a_duplicate(result):
    same_day = [w for w in result.workouts if w["date"] == "2026-08-20"]
    assert len(same_day) == 2


def test_non_overlapping_runs_have_no_overlap():
    from datetime import datetime, timedelta
    start = datetime(2026, 8, 20, 6, 0)
    morning = {"started": start, "ended": start + timedelta(minutes=32)}
    evening = {"started": start + timedelta(hours=12), "ended": start + timedelta(hours=12, minutes=44)}
    assert overlap_ratio(morning, evening) == 0.0


# --- series -----------------------------------------------------------------

def test_heart_rate_samples_land_in_the_right_workout(result):
    long_run, easy = one(result, "UUID-LONG"), one(result, "UUID-EASY")
    assert len(long_run["hr_samples"]) == 235 * 2      # one every 30s
    assert len(easy["hr_samples"]) == 51 * 2
    assert long_run["hr_samples"][0][0] == 0           # offsets start at zero
    assert all(off >= 0 for off, _ in long_run["hr_samples"])


def test_resting_samples_outside_every_workout_are_dropped(result):
    total = sum(len(w["hr_samples"]) for w in result.workouts)
    assert total == result.hr_series_found == (235 + 51) * 2


def test_route_points_are_parsed_with_units_converted(result):
    long_run = one(result, "UUID-LONG")
    assert long_run["route_points"] == 40
    offset, lat, lon, altitude_ft, speed_mph = long_run["route"][0]
    assert 41 < lat < 42 and -88 < lon < -87
    assert altitude_ft == pytest.approx(596, abs=2)    # ~181.6 m, converted
    assert speed_mph == pytest.approx(6.49, abs=0.01)  # 2.9 m/s


def test_a_workout_with_no_route_has_none(result):
    assert one(result, "UUID-EASY")["route"] == []


def test_gpx_with_no_points_is_survivable():
    assert parse_gpx(b"<gpx></gpx>", None) == []
    assert parse_gpx(b"not xml at all", None) == []


def test_routes_can_be_skipped(export_zip):
    lean = load_export(export_zip, with_routes=False)
    assert all(w["route_points"] == 0 for w in lean.workouts)
    assert len(lean.workouts) == 6      # everything else still there


# --- merging into the plan --------------------------------------------------

@pytest.fixture
def planned(tmp_path):
    conn = connect(tmp_path / "m.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO runs (run_key, source, date, status, distance_mi, workout_type,"
        " notes, week_number, imported_at) VALUES (?, 'sheet-plan', ?, ?, ?, ?, ?, ?, 'x')",
        [("sheet-plan:2026-08-22:saturday", "2026-08-22", "completed", 18.0, "Long",
          "18 Miles", 33),
         ("sheet-plan:2026-08-20:thursday", "2026-08-20", "planned", 9.0,
          "Marathon pace", "9 Miles (6 Miles @ MP)", 33)])
    conn.commit()
    return conn


def match_for(conn, workout):
    """What the week-level matcher would pair this workout with."""
    monday, sunday = week_bounds(workout["date"])
    rows = plan_candidates(conn, monday, sunday)
    return assign_week([workout], rows)[0][1]


def merge(conn, workout):
    _, row, note = assign_week([workout], plan_candidates(conn, *week_bounds(workout["date"])))[0]
    return merge_workout(conn, workout, row=row, note=note)


def row(conn, **where):
    key, value = next(iter(where.items()))
    r = conn.execute(f"SELECT * FROM runs WHERE {key} = ?", (value,)).fetchone()
    return dict(r) if r else None


def test_merge_keeps_the_plans_intent_and_takes_healths_numbers(planned, result):
    merge(planned, one(result, "UUID-LONG"))
    merged = row(planned, health_id="UUID-LONG")
    assert merged["distance_mi"] == pytest.approx(18.02)   # Health measured
    assert merged["prior_distance_mi"] == 18.0             # what the plan said
    assert merged["avg_hr"] == 149
    assert merged["week_number"] == 33                     # plan's, untouched
    assert merged["notes"] == "18 Miles"
    assert merged["workout_type"] == "Long"
    assert merged["source"] == "sheet-plan"
    assert merged["measured_source"] == "apple-health"


def test_merge_completes_a_planned_run(planned, result):
    workouts = sorted((w for w in result.workouts if w["date"] == "2026-08-20"),
                      key=lambda w: -w["duration_sec"])
    merge(planned, workouts[0])
    assert row(planned, health_id=workouts[0]["health_id"])["status"] == "completed"


def test_second_run_of_a_two_a_day_gets_its_own_row(planned, result):
    workouts = sorted((w for w in result.workouts if w["date"] == "2026-08-20"),
                      key=lambda w: -w["duration_sec"])
    for workout in workouts:
        merge(planned, workout)
    rows = planned.execute("SELECT * FROM runs WHERE date = '2026-08-20'").fetchall()
    assert len(rows) == 2
    sources = {r["source"] for r in rows}
    assert sources == {"sheet-plan", "apple-health"}


def test_a_run_with_no_plan_row_is_inserted(planned, result):
    _, outcome = merge(planned, one(result, "UUID-OLD"))
    assert outcome == "inserted"
    inserted = row(planned, health_id="UUID-OLD")
    assert inserted["source"] == "apple-health"
    assert inserted["week_number"] is None
    assert inserted["status"] == "completed"


def test_re_importing_the_same_workout_changes_nothing(planned, result):
    long_run = one(result, "UUID-LONG")
    merge_workout(planned, long_run)
    before = planned.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    samples_before = planned.execute("SELECT COUNT(*) FROM hr_samples").fetchone()[0]
    _, outcome = merge(planned, long_run)
    assert outcome == "updated"
    assert planned.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == before
    # Series are replaced, never appended.
    assert planned.execute("SELECT COUNT(*) FROM hr_samples").fetchone()[0] == samples_before


def test_samples_are_stored_against_the_run(planned, result):
    long_run = one(result, "UUID-LONG")
    run_id, _ = merge(planned, long_run)
    hr = planned.execute("SELECT COUNT(*) FROM hr_samples WHERE run_id = ?", (run_id,)).fetchone()[0]
    route = planned.execute("SELECT COUNT(*) FROM route_points WHERE run_id = ?", (run_id,)).fetchone()[0]
    assert hr == 470 and route == 40


# --- the sheet must not take a measured run back ----------------------------

def reingest_sheet(conn, **overrides):
    values = {c: None for c in RUN_COLUMNS}
    values.update(run_key="sheet-plan:2026-08-22:saturday", source="sheet-plan",
                  date="2026-08-22", status="completed", distance_mi=18.0,
                  duration_sec=14124, workout_type="Long", notes="18 Miles",
                  week_number=33, imported_at="2026-09-01")
    values.update(overrides)
    conn.execute(run_upsert(), values)
    conn.commit()


def test_a_sheet_reimport_cannot_overwrite_health_measurements(planned, result):
    merge(planned, one(result, "UUID-LONG"))
    reingest_sheet(planned)
    after = row(planned, health_id="UUID-LONG")
    assert after["distance_mi"] == pytest.approx(18.02)   # not the sheet's 18.0
    assert after["duration_sec"] == 235 * 60              # not the sheet's 14124
    assert after["avg_hr"] == 149                         # a blank cell must not erase it


def test_a_sheet_reimport_still_refreshes_intent(planned, result):
    merge(planned, one(result, "UUID-LONG"))
    reingest_sheet(planned, workout_type="Race", week_number=34, notes="18 Miles (race pace)")
    after = row(planned, health_id="UUID-LONG")
    assert after["workout_type"] == "Race"
    assert after["week_number"] == 34
    assert after["notes"] == "18 Miles (race pace)"
    assert after["distance_mi"] == pytest.approx(18.02)   # measurement still safe


# --- runs that happened on a different day than the plan said ---------------
#
# The plan pins every workout to a weekday; real weeks slide. Without matching
# across the days of a week the same run lands twice -- the sheet's row and the
# watch's -- and the week's mileage is inflated by a whole run.

def test_week_bounds_are_monday_to_sunday():
    assert week_bounds("2026-08-22") == ("2026-08-17", "2026-08-23")
    assert week_bounds("2026-08-17") == ("2026-08-17", "2026-08-23")
    assert week_bounds("2026-08-23") == ("2026-08-17", "2026-08-23")


@pytest.fixture
def week(tmp_path):
    """A plan week with a Saturday 11-miler nobody ran on Saturday."""
    conn = connect(tmp_path / "w.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO runs (run_key, source, date, status, distance_mi, workout_type,"
        " notes, week_number, imported_at) VALUES (?, 'sheet-plan', ?, ?, ?, ?, ?, 27, 'x')",
        [("sheet-plan:2026-07-11:saturday", "2026-07-11", "completed", 11.0, "Long",
          "11 Miles"),
         ("sheet-plan:2026-07-06:monday", "2026-07-06", "completed", 4.0, "Easy",
          "4 Miles Easy")])
    conn.commit()
    return conn


def measured(date, miles, minutes, health_id="H1"):
    return {"health_id": health_id, "date": date, "distance_mi": miles,
            "duration_sec": minutes * 60, "started_at": f"{date}T06:00:00",
            "pace_sec_per_mi": minutes * 60 / miles, "measured_source": "apple-health",
            "source_name": "Apple Watch", "route_points": 0,
            "hr_samples": [], "route": []}


def test_a_run_done_a_day_early_matches_its_plan_row(week):
    workout = measured("2026-07-10", 11.01, 110)          # ran Friday
    run_id, outcome = merge(week, workout)                 # plan said Saturday
    assert outcome == "shifted"
    row_ = week.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row_["run_key"] == "sheet-plan:2026-07-11:saturday"
    assert row_["distance_mi"] == pytest.approx(11.01)
    assert row_["prior_distance_mi"] == 11.0
    assert row_["date"] == "2026-07-10"                    # the day it happened
    assert week.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2


def test_a_different_distance_is_not_treated_as_the_same_run(week):
    _, outcome = merge(week, measured("2026-07-10", 7.0, 70))
    assert outcome == "inserted"
    assert week.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 3


def test_a_run_in_a_different_week_is_never_claimed(week):
    _, outcome = merge(week, measured("2026-07-13", 11.0, 110))
    assert outcome == "inserted"


def test_a_plan_row_is_only_claimed_once(week):
    merge(week, measured("2026-07-10", 11.01, 110, "H1"))
    _, outcome = merge(week, measured("2026-07-09", 11.02, 111, "H2"))
    assert outcome == "inserted"


def test_distance_decides_the_pairing_not_the_weekday(week):
    """The bug this replaced: a 14-mile run landing in the slot that said 6.

    Both runs happen in the same week, neither on its planned day. Matching by
    day would put Monday's 11-miler into Monday's 4-mile row and leave the
    Saturday 11-mile row to be double-counted.
    """
    rows = plan_candidates(week, *week_bounds("2026-07-06"))
    long_run = measured("2026-07-06", 11.02, 110, "H-LONG")   # Monday, but 11 miles
    easy = measured("2026-07-08", 4.01, 40, "H-EASY")         # Wednesday, 4 miles
    paired = {w["health_id"]: (r["notes"] if r else None)
              for w, r, _ in assign_week([long_run, easy], rows)}
    assert paired["H-LONG"] == "11 Miles"
    assert paired["H-EASY"] == "4 Miles Easy"


def test_a_distance_match_always_outranks_a_same_day_mismatch():
    """A 14-mile run must not claim the slot that said 6 just because of the date."""
    six_that_day = {"id": 1, "date": "2026-07-11", "distance_mi": 6.0}
    fourteen_later = {"id": 2, "date": "2026-07-13", "distance_mi": 14.0}
    long_run = {"date": "2026-07-11", "distance_mi": 14.01}
    assert pair_cost(long_run, fourteen_later) < pair_cost(long_run, six_that_day)


def test_an_implausible_distance_on_another_day_is_no_match():
    plan = {"id": 1, "date": "2026-07-11", "distance_mi": 6.0}
    assert pair_cost({"date": "2026-07-09", "distance_mi": 14.0}, plan) is None
    assert pair_cost({"date": "2026-07-09", "distance_mi": 6.1}, plan) is not None


def test_a_run_outside_the_week_never_matches():
    plan = {"id": 1, "date": "2026-07-11", "distance_mi": 6.0}
    assert pair_cost({"date": "2026-07-04", "distance_mi": 6.0}, plan) is None


def test_pair_cost_needs_the_exact_day_when_the_plan_has_no_distance():
    plan = {"id": 1, "date": "2026-07-11", "distance_mi": None}
    assert pair_cost({"date": "2026-07-11", "distance_mi": 6.0}, plan) == 0
    assert pair_cost({"date": "2026-07-10", "distance_mi": 6.0}, plan) is None
