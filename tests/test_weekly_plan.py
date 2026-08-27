"""Tests for the week-per-row plan loader.

The fixture mirrors the real sheet's quirks: a Time column per block rather than
per day, MM:SS times with a junk third component, an explicit zero for a run that
didn't happen, and a race written into a rest-day column.
"""

import pytest

from ingest.weekly_plan_loader import (
    classify, day_blocks, load_weekly_plan, looks_like_weekly_plan,
    parse_day_time, parse_total_time, parse_workout_distance,
)

HEADER = ("Week,Date,Monday,Time,Tuesday,Time,Wednesday,Thursday,Time,"
          "Friday,Saturday,Time,Sunday,Weight,Total Miles,Total Time\n")

SHEET = HEADER + (
    # ordinary week: four runs, all on their own days
    "1,1/5/2026,2 Miles Easy,30:00:00,3 Miles,36:00:00,Rest,Nike 1?2?3 Go,30:00:00,"
    "Rest,4 Miles,60:00:00,Rest,267,10.5,2:36:00\n"
    # long run slipped to Friday; Saturday is Rest but holds the time
    "8,2/23/2026,3 Miles Easy,41:50:00,5 Miles,72:00:00,Rest,N/A,0,"
    "10 Miles,Rest,139:35:00,Rest,N/A,18,4:13:25\n"
    # race on Sunday, which has no Time column of its own
    "11,3/16/2026,3 Miles Easy,42:31:00,6.2 Miles,77:17:00,Rest,Rock and Roller (2.6),33:25:00,"
    "Rest,Rest,66:47:00,Shamrock Shuffle 5 Miles,N/A,16.8,3:40:00\n"
    # two runs skipped: the time was entered as zero
    "31,8/3/2026,4 Miles Easy,0:00:00,8 Miles,0:00:00,Rest,7 Miles (3 Miles @ MP),90:32:00,"
    "Rest,16 Miles,233:33:00,Rest,N/A,23,5:24:05\n"
    # future week: no times recorded yet
    "40,10/5/2026,4 Miles Easy,,Rest,,Rest,3 Miles Easy,,Rest,Rest,,Chicago Marathon,,,\n"
    # trailer row carrying grand totals, with no date
    ",,,,,,,,,,,,,,667.63,148:03:26\n"
)


@pytest.fixture
def plan(tmp_path):
    path = tmp_path / "plan.csv"
    path.write_text(SHEET)
    return load_weekly_plan(path)


def by_date(plan, iso):
    return next(r for r in plan.rows if r["date"] == iso)


def test_shape_detection():
    assert looks_like_weekly_plan(HEADER.strip().split(","))
    assert not looks_like_weekly_plan(["Date", "Distance", "Duration", "Notes"])


def test_time_columns_group_the_days_before_them():
    blocks, trailing = day_blocks(HEADER.strip().split(","))
    assert [[n for n, _ in days] for days, _ in blocks] == [
        ["Monday"], ["Tuesday"], ["Wednesday", "Thursday"], ["Friday", "Saturday"]]
    assert [n for n, _ in trailing] == ["Sunday"]


@pytest.mark.parametrize("value,expected", [
    ("30:00:00", 1800),      # thirty minutes, not thirty hours
    ("139:35:00", 8375),
    ("151:31:31", 9091),     # third component is debris either way
    ("0:00:00", 0),
    ("0", 0),
    ("", None),
])
def test_day_times_are_mm_ss(value, expected):
    assert parse_day_time(value) == expected


def test_weekly_total_is_ordinary_hms():
    assert parse_total_time("4:13:25") == 15205


@pytest.mark.parametrize("text,expected", [
    ("4 Miles Easy", 4.0),
    ("6.2 Miles", 6.2),
    ("Power Pyramid (2.8)", 2.8),
    ("Hill Repeats (8 Miles with 8 Repeats)", 8.0),
    ("13.1 - Cham Half", 13.1),
    ("Shamrock Shuffle 5 Miles", 5.0),
    ("Chicago Marathon", 26.2),
    ("Chicago Half Marathon", 13.1),
    ("Rest", None),
    ("Vacation", None),
    ("Nike 1,2,3 Go", None),
])
def test_distance_read_out_of_the_workout_text(text, expected):
    assert parse_workout_distance(text) == expected


def test_run_that_slipped_to_friday_keeps_fridays_date(plan):
    friday = by_date(plan, "2026-02-27")
    assert friday["distance_mi"] == 10.0
    assert friday["duration_sec"] == 8375
    assert friday["status"] == "completed"
    # Saturday was Rest, so it produced nothing of its own
    assert not [r for r in plan.rows if r["date"] == "2026-02-28"]


def test_race_in_a_rest_day_column_gets_the_blocks_time(plan):
    sunday = by_date(plan, "2026-03-22")
    assert sunday["distance_mi"] == 5.0
    assert sunday["duration_sec"] == 4007
    assert sunday["workout_type"] == "Race"


def test_zero_time_means_the_run_was_skipped(plan):
    assert by_date(plan, "2026-08-03")["status"] == "skipped"
    assert by_date(plan, "2026-08-04")["status"] == "skipped"
    assert by_date(plan, "2026-08-06")["status"] == "completed"


def test_future_weeks_are_planned_not_completed(plan):
    marathon = by_date(plan, "2026-10-11")
    assert marathon["status"] == "planned"
    assert marathon["distance_mi"] == 26.2
    assert marathon["workout_type"] == "Race"


def test_unknown_distance_is_backed_out_of_the_weekly_total(plan):
    guided = by_date(plan, "2026-01-08")
    assert guided["distance_mi"] == 1.5
    assert plan.derived_distances


def test_completed_runs_reconcile_to_the_sheets_own_totals(plan):
    for week in plan.weeks:
        if week["sheet_seconds"] is None:
            continue
        mine = sum(r["duration_sec"] or 0 for r in plan.rows
                   if r["status"] == "completed"
                   and r["week_number"] == week["week_number"])
        assert abs(mine - week["sheet_seconds"]) <= 1


def test_trailer_row_without_a_date_is_ignored(plan):
    assert len(plan.weeks) == 5
    assert all(w["week_start"] for w in plan.weeks)


def test_weekly_extras_are_kept(plan):
    first = plan.weeks[0]
    assert first["weight_lb"] == 267
    assert first["sheet_miles"] == 10.5


def test_classification():
    assert classify("4 Miles Easy", distance=4) == "Easy"
    assert classify("Yasso 800's (9 Miles with 4 x 800)", distance=9) == "Speed"
    assert classify("Chicago Marathon", distance=26.2) == "Race"
    assert classify("16 Miles", distance=16) == "Long"
    assert classify("3 Miles", distance=3) == "Run"
