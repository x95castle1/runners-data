"""VO2 max: storing the readings, lining them up with runs, and trending them."""

import pytest

from app import stats
from ingest.cli import attach_vo2, store_metrics
from ingest.schema import connect, init_db


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "v.db")
    init_db(c)
    return c


def add_readings(conn, pairs):
    store_metrics(conn, [
        {"metric": "vo2_max", "recorded_at": f"{day} 08:00:00 -0500", "date": day,
         "value": value, "unit": "mL/min·kg", "source_name": "Apple Watch"}
        for day, value in pairs])


def add_run(conn, day, run_key=None):
    conn.execute(
        "INSERT INTO runs (run_key, source, date, status, distance_mi, imported_at)"
        " VALUES (?, 'apple-health', ?, 'completed', 5.0, 'x')",
        (run_key or f"r:{day}", day))
    conn.commit()


# --- storing ----------------------------------------------------------------

def test_readings_are_stored_once_per_timestamp(conn):
    add_readings(conn, [("2026-01-05", 34.9), ("2026-01-12", 35.1)])
    add_readings(conn, [("2026-01-05", 34.9)])          # a re-import
    assert conn.execute("SELECT COUNT(*) FROM health_metrics").fetchone()[0] == 2


def test_several_readings_in_a_day_are_averaged(conn):
    store_metrics(conn, [
        {"metric": "vo2_max", "recorded_at": f"2026-01-05 0{h}:00:00 -0500",
         "date": "2026-01-05", "value": v, "unit": "x", "source_name": "w"}
        for h, v in ((7, 34.0), (8, 36.0))])
    assert stats.vo2_series(conn)[0]["value"] == pytest.approx(35.0)


# --- lining up with runs ----------------------------------------------------

def test_a_same_day_reading_is_used(conn):
    add_readings(conn, [("2026-03-01", 37.5)])
    add_run(conn, "2026-03-01")
    tally = attach_vo2(conn)
    row = conn.execute("SELECT vo2_max, vo2_max_date FROM runs").fetchone()
    assert (row["vo2_max"], row["vo2_max_date"]) == (37.5, "2026-03-01")
    assert tally["same_day"] == 1 and tally["carried"] == 0


def test_the_most_recent_earlier_reading_carries_forward(conn):
    """Apple's figure is a slow rolling estimate, so the current one is the right
    reading for a day without its own."""
    add_readings(conn, [("2026-03-01", 37.5)])
    add_run(conn, "2026-03-04")
    tally = attach_vo2(conn)
    row = conn.execute("SELECT vo2_max, vo2_max_date FROM runs").fetchone()
    assert row["vo2_max"] == 37.5
    assert row["vo2_max_date"] == "2026-03-01"   # says where it came from
    assert tally["carried"] == 1


def test_a_stale_reading_is_not_carried(conn):
    add_readings(conn, [("2026-01-01", 37.5)])
    add_run(conn, "2026-06-01")
    tally = attach_vo2(conn)
    assert conn.execute("SELECT vo2_max FROM runs").fetchone()["vo2_max"] is None
    assert tally["none"] == 1


def test_a_later_reading_is_never_used_for_an_earlier_run(conn):
    add_readings(conn, [("2026-03-10", 40.0)])
    add_run(conn, "2026-03-01")
    attach_vo2(conn)
    assert conn.execute("SELECT vo2_max FROM runs").fetchone()["vo2_max"] is None


def test_runs_predating_every_reading_get_nothing(conn):
    add_readings(conn, [("2026-03-01", 37.5)])
    add_run(conn, "2019-05-05")
    assert attach_vo2(conn)["none"] == 1


# --- summary and trend ------------------------------------------------------

def test_summary_reports_latest_and_best(conn):
    add_readings(conn, [("2026-01-05", 34.9), ("2026-05-01", 41.0), ("2026-08-01", 39.5)])
    summary = stats.vo2_summary(conn)
    assert summary["latest"] == 39.5 and summary["latest_date"] == "2026-08-01"
    assert summary["best"] == 41.0 and summary["best_date"] == "2026-05-01"
    # Unscoped, the best in view is by definition the best on record.
    assert summary["is_lifetime_best"] is True


def test_a_current_peak_is_flagged(conn):
    add_readings(conn, [("2026-01-05", 34.9), ("2026-08-01", 41.7)])
    summary = stats.vo2_summary(conn)
    assert summary["is_lifetime_best"] is True
    assert summary["latest"] == summary["best"]


def test_a_scoped_best_knows_it_is_not_the_lifetime_best(conn):
    add_readings(conn, [("2021-06-01", 44.0), ("2026-08-01", 41.7)])
    scoped = stats.vo2_summary(conn, since="2026-01-01")
    assert scoped["best"] == 41.7
    assert scoped["is_lifetime_best"] is False      # 2021 was higher


def test_no_readings_means_no_summary(conn):
    assert stats.vo2_summary(conn) is None


def test_a_rising_estimate_trends_upward(conn):
    add_readings(conn, [(f"2026-0{m}-01", 34.0 + m) for m in range(1, 9)])
    trend = stats.vo2_summary(conn)["trend"]
    assert trend["per_month"] > 0.9
    assert trend["fit"] == pytest.approx(1.0, abs=0.01)


def test_the_shared_trend_still_serves_pace():
    """pace_trend wraps linear_trend and flips the sign, since falling pace is
    an improvement while rising VO2 max is."""
    rows = [{"date": f"2026-0{m}-01", "pace_sec_per_mi": 840 - m * 10}
            for m in range(1, 9)]
    assert stats.pace_trend(rows)["seconds_per_month"] > 0


def test_a_planned_run_gets_no_figure(conn):
    """It hasn't happened; a future date would otherwise pick up today's estimate."""
    add_readings(conn, [("2026-08-25", 41.7)])
    conn.execute(
        "INSERT INTO runs (run_key, source, date, status, distance_mi, imported_at)"
        " VALUES ('p', 'sheet-plan', '2026-09-10', 'planned', 9.0, 'x')")
    conn.commit()
    attach_vo2(conn)
    assert conn.execute("SELECT vo2_max FROM runs").fetchone()["vo2_max"] is None


def test_a_figure_is_cleared_if_a_run_goes_back_on_the_plan(conn):
    add_readings(conn, [("2026-03-01", 37.5)])
    add_run(conn, "2026-03-01")
    attach_vo2(conn)
    assert conn.execute("SELECT vo2_max FROM runs").fetchone()["vo2_max"] == 37.5
    conn.execute("UPDATE runs SET status = 'planned'")
    conn.commit()
    attach_vo2(conn)
    assert conn.execute("SELECT vo2_max FROM runs").fetchone()["vo2_max"] is None


# --- steps and calories -----------------------------------------------------

def add_completed(conn, day, **fields):
    columns = ["run_key", "source", "date", "status", "imported_at", *fields]
    values = [f"r:{day}", "apple-health", day, "completed", "x", *fields.values()]
    conn.execute(f"INSERT INTO runs ({', '.join(columns)})"
                 f" VALUES ({', '.join('?' * len(columns))})", values)
    conn.commit()


def test_summary_totals_steps_and_calories(conn):
    add_completed(conn, "2026-03-01", distance_mi=5.0, duration_sec=3000,
                  steps=7000, calories=600.5)
    add_completed(conn, "2026-03-03", distance_mi=3.0, duration_sec=1800,
                  steps=4200, calories=350.25)
    summary = stats.summary(conn)
    assert summary["steps"] == 11200
    assert summary["calories"] == pytest.approx(950.75)


def test_planned_runs_are_left_out_of_the_totals(conn):
    add_completed(conn, "2026-03-01", distance_mi=5.0, duration_sec=3000,
                  steps=7000, calories=600)
    conn.execute(
        "INSERT INTO runs (run_key, source, date, status, steps, calories,"
        " imported_at) VALUES ('p', 'sheet-plan', '2026-09-01', 'planned',"
        " 9999, 999, 'x')")
    conn.commit()
    assert stats.summary(conn)["steps"] == 7000


def test_weekly_carries_steps_and_calories(conn):
    add_completed(conn, "2026-03-02", distance_mi=5.0, duration_sec=3000,
                  steps=7000, calories=600)      # a Monday
    add_completed(conn, "2026-03-04", distance_mi=3.0, duration_sec=1800,
                  steps=4200, calories=350)      # same week
    week = [w for w in stats.weekly(conn, weeks=0) if w["week_start"] == "2026-03-02"][0]
    assert week["steps"] == 11200
    assert week["calories"] == 950


def test_a_week_with_no_runs_reports_zero_not_none(conn):
    """The chart plots every week in the range, so gaps have to be numbers."""
    add_completed(conn, "2026-03-02", distance_mi=5.0, duration_sec=3000,
                  steps=7000, calories=600)
    add_completed(conn, "2026-03-23", distance_mi=5.0, duration_sec=3000,
                  steps=7000, calories=600)
    weeks = stats.weekly(conn, weeks=0)
    empty = [w for w in weeks if w["week_start"] == "2026-03-09"][0]
    assert empty["steps"] == 0 and empty["calories"] == 0
