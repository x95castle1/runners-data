"""Per-split weather: cache keys, interpolation, and the wind maths.

No network here -- rows are inserted directly. The parts worth testing are the
ones that are quietly wrong rather than loudly broken: a compass average that
goes the long way round, or a headwind that comes out as a tailwind.
"""

from datetime import datetime

import pytest

from app import stats
from app.stats import _blend_angle, bearing, headwind, weather_at
from ingest.schema import connect, init_db
from ingest.weather import place_key


# --- the cache key ----------------------------------------------------------

def test_nearby_runs_share_one_weather_cell():
    """Eight cells for one town means eight lookups returning the same weather."""
    town = {place_key(40.50, -88.93), place_key(40.52, -88.98), place_key(40.50, -88.98)}
    assert len(town) <= 2
    assert place_key(41.88, -87.63) not in town      # Chicago stays distinct


def test_place_key_is_coarser_than_a_front_door():
    # A tenth of a degree is about 11 km -- the resolution of the source grid.
    assert place_key(40.5061234, -88.9876543) == "40.5,-89.0"


# --- interpolating between hourly readings ----------------------------------

def rows(*pairs):
    return [{"hour_ts": ts, "temp_f": t, "apparent_f": t, "humidity_pct": h,
             "dew_point_f": None, "wind_mph": w, "wind_dir_deg": d, "precip_in": 0}
            for ts, t, h, w, d in pairs]


HOURS = rows(
    ("2026-08-22T05:00", 63.5, 95, 6.7, 264),
    ("2026-08-22T06:00", 63.7, 95, 7.1, 281),
    ("2026-08-22T07:00", 64.2, 96, 6.1, 303),
)


def test_halfway_between_two_hours():
    at = weather_at(HOURS, datetime.fromisoformat("2026-08-22T05:30"))
    assert at["temp_f"] == pytest.approx(63.6, abs=0.01)
    assert at["wind_mph"] == pytest.approx(6.9, abs=0.01)


def test_before_and_after_the_series_clamp():
    assert weather_at(HOURS, datetime.fromisoformat("2026-08-22T03:00"))["temp_f"] == 63.5
    assert weather_at(HOURS, datetime.fromisoformat("2026-08-22T23:00"))["temp_f"] == 64.2


def test_no_readings_means_no_weather():
    assert weather_at([], datetime.fromisoformat("2026-08-22T05:30")) is None


# --- compass angles ---------------------------------------------------------

def test_wind_direction_interpolates_the_short_way_round():
    """Averaging 350 and 10 naively gives 180 -- due south, the exact opposite."""
    assert _blend_angle(350, 10, 0.5) == pytest.approx(0, abs=0.001)
    assert _blend_angle(10, 350, 0.5) == pytest.approx(0, abs=0.001)


def test_ordinary_angles_still_interpolate_normally():
    assert _blend_angle(264, 284, 0.5) == pytest.approx(274)


def test_bearing_points_the_way_you_ran():
    north = bearing({"lat": 40.50, "lon": -88.95}, {"lat": 40.52, "lon": -88.95})
    east = bearing({"lat": 40.50, "lon": -88.95}, {"lat": 40.50, "lon": -88.90})
    assert north == pytest.approx(0, abs=1)
    assert east == pytest.approx(90, abs=1)


# --- the wind you actually feel ---------------------------------------------

def test_wind_from_dead_ahead_is_a_full_headwind():
    # Running north into a wind blowing from the north.
    assert headwind(10, 0, 0) == pytest.approx(10)


def test_wind_from_behind_is_a_full_tailwind():
    assert headwind(10, 180, 0) == pytest.approx(-10)


def test_a_crosswind_is_neither():
    assert headwind(10, 90, 0) == pytest.approx(0, abs=1e-9)


def test_the_component_scales_with_the_angle():
    assert headwind(10, 45, 0) == pytest.approx(7.07, abs=0.01)


def test_missing_inputs_give_no_answer():
    assert headwind(None, 0, 0) is None
    assert headwind(10, None, 0) is None
    assert headwind(10, 0, None) is None


# --- end to end through splits ----------------------------------------------

@pytest.fixture
def run_with_weather(tmp_path):
    conn = connect(tmp_path / "w.db")
    init_db(conn)
    conn.execute(
        "INSERT INTO runs (run_key, source, date, status, distance_mi, duration_sec,"
        " started_at, weather_place, imported_at) VALUES"
        " ('r', 'apple-health', '2026-08-22', 'completed', 2.0, 1800,"
        "  '2026-08-22T05:00:00-05:00', '40.5,-89.0', 'x')")
    run_id = conn.execute("SELECT id FROM runs").fetchone()["id"]
    # Two miles due north, one point every ~0.1 mile.
    points = []
    for i in range(41):
        points.append((run_id, i * 45, 40.50 + i * 0.00145, -88.95, 700.0, 6.0))
    conn.executemany(
        "INSERT INTO route_points (run_id, offset_sec, lat, lon, altitude_ft, speed_mph)"
        " VALUES (?, ?, ?, ?, ?, ?)", points)
    conn.executemany(
        "INSERT INTO weather_hours (place, hour_ts, temp_f, apparent_f, humidity_pct,"
        " wind_mph, wind_dir_deg, fetched_at) VALUES ('40.5,-89.0', ?, ?, ?, ?, ?, ?, 'x')",
        [("2026-08-22T05:00", 63.5, 64.9, 95, 10.0, 0),
         ("2026-08-22T06:00", 65.5, 67.0, 92, 10.0, 0)])
    conn.commit()
    return conn


def test_splits_carry_the_conditions_at_the_time(run_with_weather):
    out = stats.splits(run_with_weather, 1)
    assert len(out) >= 2
    assert out[0]["apparent_f"] is not None
    # The morning warms as the run goes on.
    assert out[-1]["temp_f"] > out[0]["temp_f"]


def test_running_north_into_a_northerly_reads_as_headwind(run_with_weather):
    out = stats.splits(run_with_weather, 1)
    assert out[0]["heading_deg"] == pytest.approx(0, abs=2)
    assert out[0]["headwind_mph"] == pytest.approx(10, abs=0.2)


def test_a_run_with_no_weather_cell_still_has_splits(tmp_path):
    conn = connect(tmp_path / "n.db")
    init_db(conn)
    conn.execute(
        "INSERT INTO runs (run_key, source, date, status, distance_mi, duration_sec,"
        " started_at, imported_at) VALUES ('r', 'apple-health', '2026-08-22',"
        " 'completed', 2.0, 1800, '2026-08-22T05:00:00-05:00', 'x')")
    conn.executemany(
        "INSERT INTO route_points (run_id, offset_sec, lat, lon, altitude_ft, speed_mph)"
        " VALUES (1, ?, ?, -88.95, 700.0, 6.0)",
        [(i * 45, 40.50 + i * 0.00145) for i in range(41)])
    conn.commit()
    out = stats.splits(conn, 1)
    assert out and out[0].get("apparent_f") is None


# --- the part-mile finish ---------------------------------------------------

def test_the_final_partial_split_gets_a_deviation(tmp_path):
    """It used to be appended after the deviation pass and rendered as undefined,
    which took the whole page down with a 500."""
    conn = connect(tmp_path / "p.db")
    init_db(conn)
    conn.execute(
        "INSERT INTO runs (run_key, source, date, status, distance_mi, duration_sec,"
        " started_at, imported_at) VALUES ('r', 'apple-health', '2026-08-22',"
        " 'completed', 1.5, 1200, '2026-08-22T05:00:00-05:00', 'x')")
    conn.executemany(
        "INSERT INTO route_points (run_id, offset_sec, lat, lon, altitude_ft, speed_mph)"
        " VALUES (1, ?, ?, -88.95, 700.0, 6.0)",
        [(i * 40, 40.50 + i * 0.00073) for i in range(31)])
    conn.commit()

    out = stats.splits(conn, 1)
    partial = [s for s in out if not s["mile"]]
    assert partial, "a 1.5 mile run should end with a part-mile split"
    # Every split carries the key, so the template never meets an undefined value.
    assert all("deviation_sec" in s for s in out)
    assert partial[0]["deviation_sec"] is not None


def test_every_split_carries_the_keys_the_page_reads(tmp_path):
    conn = connect(tmp_path / "k.db")
    init_db(conn)
    conn.execute(
        "INSERT INTO runs (run_key, source, date, status, distance_mi, duration_sec,"
        " started_at, imported_at) VALUES ('r', 'apple-health', '2026-08-22',"
        " 'completed', 1.5, 1200, '2026-08-22T05:00:00-05:00', 'x')")
    conn.executemany(
        "INSERT INTO route_points (run_id, offset_sec, lat, lon, altitude_ft, speed_mph)"
        " VALUES (1, ?, ?, -88.95, 700.0, 6.0)",
        [(i * 40, 40.50 + i * 0.00073) for i in range(31)])
    conn.commit()
    for split in stats.splits(conn, 1):
        for key in ("mile", "seconds", "pace_sec_per_mi", "elevation_change_ft",
                    "avg_hr", "deviation_sec"):
            assert key in split, f"{key} missing from a split"


# --- the pace trend ---------------------------------------------------------

def trend_rows(start_pace, per_day, days, step=7):
    from datetime import date, timedelta

    origin = date(2026, 1, 5)
    return [{"date": (origin + timedelta(days=d)).isoformat(),
             "pace_sec_per_mi": start_pace + per_day * d}
            for d in range(0, days + 1, step)]


def test_a_steady_improvement_is_measured():
    # Two seconds per mile faster every week -> about 8.7 a month.
    rows = trend_rows(840, -2 / 7, 210)
    t = stats.pace_trend(rows)
    assert t["seconds_per_month"] == pytest.approx(8.7, abs=0.1)
    assert t["fit"] == pytest.approx(1.0, abs=1e-6)     # a perfect line
    assert t["start_pace"] > t["end_pace"]              # ending faster


def test_getting_slower_reads_as_negative():
    assert stats.pace_trend(trend_rows(720, 1 / 7, 210))["seconds_per_month"] < 0


def test_no_trend_from_too_few_runs():
    assert stats.pace_trend(trend_rows(800, 0, 14, step=7)) is None


def test_no_trend_when_every_run_is_the_same_day():
    rows = [{"date": "2026-01-05", "pace_sec_per_mi": 800 + i} for i in range(10)]
    assert stats.pace_trend(rows) is None


def test_runs_without_a_pace_are_ignored():
    rows = trend_rows(840, -2 / 7, 210)
    rows += [{"date": "2026-03-01", "pace_sec_per_mi": None}] * 3
    assert stats.pace_trend(rows) is not None


def test_scatter_lowers_the_fit_but_keeps_the_slope():
    """Pace swings with workout type as much as fitness, so a modest fit is
    normal -- the number is there to say how much to trust the line."""
    rows = trend_rows(840, -2 / 7, 210)
    for i, row in enumerate(rows):
        row["pace_sec_per_mi"] += 60 if i % 2 else -60
    t = stats.pace_trend(rows)
    assert t["seconds_per_month"] == pytest.approx(8.7, abs=0.5)
    assert t["fit"] < 0.5


def test_the_line_spans_the_actual_dates():
    rows = trend_rows(840, -2 / 7, 210)
    t = stats.pace_trend(rows)
    assert t["start_date"] == rows[0]["date"]
    assert t["end_date"] == rows[-1]["date"]
    assert t["days"] == 210
