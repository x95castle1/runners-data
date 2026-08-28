"""Time in heart-rate zones, and not counting time spent standing still.

The pause handling is the substance here. Apple's `duration` excludes paused
time but the samples and GPS points span it, so anything measured off sample
timestamps counts a five-minute stop as five minutes of running unless it
subtracts the pauses.
"""

import pytest

from app import stats
from app.stats import paused_between, zones_from_reserve
from ingest.schema import connect, init_db


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "z.db")
    init_db(c)
    c.execute(
        "INSERT INTO runs (run_key, source, date, status, distance_mi, duration_sec,"
        " started_at, imported_at) VALUES ('r', 'apple-health', '2026-08-22',"
        " 'completed', 3.0, 1800, '2026-08-22T06:00:00-05:00', 'x')")
    c.commit()
    return c


def add_samples(conn, pairs):
    conn.executemany("INSERT INTO hr_samples (run_id, offset_sec, bpm) VALUES (1, ?, ?)",
                     pairs)
    conn.commit()


def add_pause(conn, start, end):
    conn.execute("INSERT INTO run_pauses (run_id, start_sec, end_sec) VALUES (1, ?, ?)",
                 (start, end))
    conn.commit()


# --- the thresholds ---------------------------------------------------------

def test_reserve_thresholds_match_the_watch():
    """Reproduces the thresholds the Fitness app shows for this runner.

    resting 55 / max 173 is the pair that lands on them exactly; the export's
    median resting heart rate for 2026 is 54 and the 99.9th percentile of
    recorded samples is 173, so it checks out from two directions.
    """
    assert zones_from_reserve(55, 173) == (126, 138, 149, 161)


def test_the_defaults_are_those_thresholds():
    assert stats.DEFAULT_HR_ZONES == (126, 138, 149, 161)


# --- assigning samples to zones ---------------------------------------------

def test_each_zone_gets_the_seconds_its_samples_held(conn):
    # 10s in each zone, one sample every 10s.
    add_samples(conn, [(0, 100), (10, 130), (20, 142), (30, 155), (40, 170), (50, 170)])
    zones = stats.hr_zones(conn, 1)
    assert [z["seconds"] for z in zones][:4] == [10, 10, 10, 10]
    assert [z["zone"] for z in zones] == [1, 2, 3, 4, 5]


def test_the_boundaries_are_inclusive_at_the_bottom(conn):
    add_samples(conn, [(0, 125), (10, 126), (20, 137), (30, 138), (40, 138)])
    zones = stats.hr_zones(conn, 1)
    assert zones[0]["seconds"] == 10        # 125 is still zone 1
    assert zones[1]["seconds"] == 20        # 126 and 137 are zone 2
    assert zones[2]["seconds"] >= 10        # 138 starts zone 3


def test_labels_read_the_way_the_watch_writes_them(conn):
    add_samples(conn, [(0, 100), (10, 100)])
    labels = [z["label"] for z in stats.hr_zones(conn, 1)]
    assert labels == ["<126", "126–137", "138–148", "149–160", "161+"]


def test_a_long_gap_is_not_credited_to_anyone(conn):
    """A gap that long means the watch stopped sampling, not that a zone held."""
    add_samples(conn, [(0, 100), (10, 100), (600, 100), (610, 100)])
    total = sum(z["seconds"] for z in stats.hr_zones(conn, 1))
    assert total < 60


def test_too_few_samples_means_no_zones(conn):
    add_samples(conn, [(0, 140)])
    assert stats.hr_zones(conn, 1) == []


def test_shares_add_up(conn):
    add_samples(conn, [(0, 100), (10, 130), (20, 155), (30, 155)])
    assert sum(z["share"] for z in stats.hr_zones(conn, 1)) == pytest.approx(1.0)


# --- pauses -----------------------------------------------------------------

def test_overlap_is_measured_not_guessed():
    windows = [(100, 200), (400, 500)]
    assert paused_between(windows, 0, 1000) == 200
    assert paused_between(windows, 150, 450) == 100      # half of each
    assert paused_between(windows, 0, 50) == 0
    assert paused_between(windows, 250, 350) == 0        # between the two


def test_standing_still_is_not_time_in_a_zone(conn):
    add_samples(conn, [(0, 100), (10, 100), (20, 100), (30, 100)])
    before = sum(z["seconds"] for z in stats.hr_zones(conn, 1))
    add_pause(conn, 10, 20)
    after = sum(z["seconds"] for z in stats.hr_zones(conn, 1))
    assert before - after == 10


def test_zone_time_reconciles_with_the_active_duration(conn):
    """The whole point: what the page reports as the run's time is active time,
    and the zones have to add up to it."""
    add_samples(conn, [(t, 140) for t in range(0, 1810, 10)])
    add_pause(conn, 600, 900)      # five minutes stopped
    conn.execute("UPDATE runs SET duration_sec = 1500")   # Apple's active figure
    conn.commit()
    total = sum(z["seconds"] for z in stats.hr_zones(conn, 1))
    assert total == pytest.approx(1500, abs=20)


# --- splits -----------------------------------------------------------------

def test_a_split_does_not_charge_you_for_a_pause(conn):
    """A mile with a five-minute stop in it was not a five-minute-slower mile."""
    conn.executemany(
        "INSERT INTO route_points (run_id, offset_sec, lat, lon, altitude_ft, speed_mph)"
        " VALUES (1, ?, ?, -88.95, 700, 6)",
        [(i * 30, 40.50 + i * 0.00073) for i in range(61)])
    conn.execute("UPDATE runs SET distance_mi = 3.0, duration_sec = 1800")
    conn.commit()

    without = stats.splits(conn, 1)
    add_pause(conn, 300, 600)      # five minutes inside the first mile
    with_pause = stats.splits(conn, 1)

    assert with_pause[0]["seconds"] == without[0]["seconds"] - 300
    assert with_pause[0]["pace_sec_per_mi"] < without[0]["pace_sec_per_mi"]


# --- heartbeats -------------------------------------------------------------
#
# Average heart rate is a time-weighted mean over active time, so multiplying it
# by the duration gives the same quantity as integrating the samples -- checked
# against the integration on 57 real runs, median difference under one percent --
# while covering nearly every run rather than only the densely sampled ones.

def test_beats_are_rate_times_time():
    assert stats.heartbeats(120, 3600) == 7200        # 120 bpm for an hour
    assert stats.heartbeats(133, 14124) == 31_308     # the Aug 22 long run


def test_no_rate_or_no_time_means_no_count():
    assert stats.heartbeats(None, 3600) is None
    assert stats.heartbeats(140, None) is None
    assert stats.heartbeats(0, 3600) is None


def test_the_total_adds_up_the_runs(conn):
    conn.execute("UPDATE runs SET avg_hr = 120, duration_sec = 3600")
    conn.execute(
        "INSERT INTO runs (run_key, source, date, status, avg_hr, duration_sec,"
        " imported_at) VALUES ('b', 'apple-health', '2026-08-23', 'completed',"
        " 150, 1800, 'x')")
    conn.commit()
    assert stats.total_heartbeats(conn)["beats"] == 7200 + 4500


def test_planned_runs_contribute_nothing(conn):
    conn.execute("UPDATE runs SET avg_hr = 120, duration_sec = 3600")
    conn.execute(
        "INSERT INTO runs (run_key, source, date, status, avg_hr, duration_sec,"
        " imported_at) VALUES ('p', 'sheet-plan', '2026-09-01', 'planned',"
        " 150, 1800, 'x')")
    conn.commit()
    total = stats.total_heartbeats(conn)
    assert total["beats"] == 7200 and total["runs"] == 1


def test_the_total_follows_the_scope(conn):
    conn.execute("UPDATE runs SET avg_hr = 120, duration_sec = 3600")
    conn.execute(
        "INSERT INTO runs (run_key, source, date, status, avg_hr, duration_sec,"
        " imported_at) VALUES ('old', 'apple-health', '2019-05-05', 'completed',"
        " 120, 3600, 'x')")
    conn.commit()
    assert stats.total_heartbeats(conn)["runs"] == 2
    assert stats.total_heartbeats(conn, since="2026-01-01")["runs"] == 1


def test_a_run_without_an_average_is_skipped(conn):
    conn.execute("UPDATE runs SET avg_hr = NULL, duration_sec = 3600")
    conn.commit()
    assert stats.total_heartbeats(conn) == {"beats": 0, "runs": 0}


# --- cadence and the elevation profile --------------------------------------

def add_cadence(conn, buckets):
    conn.executemany(
        "INSERT INTO cadence_samples (run_id, offset_sec, steps, span_sec)"
        " VALUES (1, ?, ?, 60)", buckets)
    conn.commit()


def test_cadence_is_steps_over_the_minute(conn):
    add_cadence(conn, [(i * 60, 150) for i in range(10)])
    series = stats.cadence_series(conn)  if False else stats.cadence_series(conn, 1)
    assert all(p["spm"] == 150 for p in series)


def test_a_paused_minute_counts_only_the_running_part(conn):
    """75 steps in the 30 seconds that were run is a cadence of 150, not 75."""
    add_cadence(conn, [(i * 60, 150) for i in range(10)])
    conn.execute("UPDATE cadence_samples SET steps = 75 WHERE offset_sec = 300")
    conn.commit()
    add_pause(conn, 300, 330)
    spm = {p["offset_sec"]: p["spm"] for p in stats.cadence_series(conn, 1)}
    assert spm[300] == 150


def test_a_minute_almost_entirely_paused_is_dropped(conn):
    add_cadence(conn, [(i * 60, 150) for i in range(10)])
    add_pause(conn, 300, 355)          # only 5 seconds of running left
    assert 300 not in {p["offset_sec"] for p in stats.cadence_series(conn, 1)}


def test_a_single_minute_standing_still_is_filtered_out(conn):
    """Stopping at a crossing without pausing reads as 44 spm, which is not a
    cadence. One minute of it shouldn't dominate the chart."""
    buckets = [(i * 60, 150) for i in range(10)]
    buckets[5] = (300, 44)
    add_cadence(conn, buckets)
    spm = {p["offset_sec"]: p["spm"] for p in stats.cadence_series(conn, 1)}
    assert spm[300] == 150


def test_a_sustained_drop_survives_the_filter(conn):
    """A real fade lasts longer than a minute and must not be smoothed away."""
    buckets = [(i * 60, 150) for i in range(6)] + [(i * 60, 120) for i in range(6, 12)]
    add_cadence(conn, buckets)
    spm = [p["spm"] for p in stats.cadence_series(conn, 1)]
    assert spm[0] == 150 and spm[-1] == 120


def test_too_few_buckets_means_no_series(conn):
    add_cadence(conn, [(0, 150), (60, 150)])
    assert stats.cadence_series(conn, 1) == []


def test_the_elevation_profile_smooths_without_moving_the_hills(conn):
    """Raw GPS altitude jitters a foot or two a reading; the hills are real."""
    import math
    conn.executemany(
        "INSERT INTO route_points (run_id, offset_sec, lat, lon, altitude_ft, speed_mph)"
        " VALUES (1, ?, 40.5, -88.95, ?, 6)",
        [(i, 800 + 40 * math.sin(i / 200) + (3 if i % 2 else -3)) for i in range(2000)])
    conn.commit()
    profile = stats.elevation_profile(conn, 1)
    heights = [p["altitude_ft"] for p in profile]
    assert 10 < max(heights) - min(heights) < 90        # the hill survives
    steps = [abs(b - a) for a, b in zip(heights, heights[1:])]
    assert max(steps) < 6                               # the jitter does not


def test_no_profile_without_altitudes(conn):
    assert stats.elevation_profile(conn, 1) == []
