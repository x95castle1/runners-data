import json
from pathlib import Path

import pytest

from ingest.csv_loader import load_csv
from ingest.schema import match_columns

SHEET = """2026 Marathon Training Log,,,,,,,
,,,,,,,
Date,Workout Type,Distance (mi),Duration,Avg Pace,Avg HR,Elevation Gain,Notes
Mon 6/1/2026,Easy,4.0,0:36:20,9:05,142,120,legs felt heavy
6/2/2026,Rest,,,,,,rest day
6/3/2026,Tempo,6.2,50:10,8:05,161,340,4mi @ tempo
6/7/2026,Long,13.1,1:58:44,9:04,149,610,longest so far
"""


@pytest.fixture
def sheet(tmp_path: Path) -> Path:
    path = tmp_path / "runs.csv"
    path.write_text(SHEET)
    return path


def test_finds_header_below_a_title_row(sheet):
    result = load_csv(sheet)
    assert result.header_row == 2
    assert result.mapping["date"] == "Date"
    assert result.mapping["distance_mi"] == "Distance (mi)"
    assert result.mapping["workout_type"] == "Workout Type"


def test_rest_days_are_skipped_not_imported(sheet):
    result = load_csv(sheet)
    assert len(result.rows) == 3
    assert result.skipped_no_activity == 1


def test_values_are_normalized(sheet):
    first = load_csv(sheet).rows[0]
    assert first["date"] == "2026-06-01"
    assert first["distance_mi"] == 4.0
    assert first["duration_sec"] == 2180
    assert first["pace_sec_per_mi"] == pytest.approx(545, abs=1)
    assert first["avg_hr"] == 142


def test_run_keys_are_stable_and_unique(sheet):
    keys = [r["run_key"] for r in load_csv(sheet).rows]
    assert len(keys) == len(set(keys))
    assert keys == [r["run_key"] for r in load_csv(sheet).rows]


def test_pace_is_derived_when_absent(tmp_path):
    path = tmp_path / "nopace.csv"
    path.write_text("Date,Distance,Duration\n2026-06-01,5,45:00\n")
    row = load_csv(path).rows[0]
    assert row["pace_sec_per_mi"] == pytest.approx(540)


def test_stale_pace_column_loses_to_measured_values(tmp_path):
    path = tmp_path / "stale.csv"
    path.write_text("Date,Distance,Duration,Pace\n2026-06-01,5,45:00,12:00\n")
    row = load_csv(path).rows[0]
    assert row["pace_sec_per_mi"] == pytest.approx(540)


def test_unmapped_columns_are_carried_along(tmp_path):
    path = tmp_path / "extra.csv"
    path.write_text("Date,Distance,Duration,Sleep Score\n2026-06-01,5,45:00,88\n")
    result = load_csv(path)
    assert "Sleep Score" in result.unmapped
    assert json.loads(result.rows[0]["extra"]) == {"Sleep Score": "88"}


def test_overrides_win_over_detection(tmp_path):
    path = tmp_path / "odd.csv"
    path.write_text("Day,How Far,How Long\n2026-06-01,5,45:00\n")
    result = load_csv(path, overrides={"distance_mi": "How Far",
                                       "duration_sec": "How Long"})
    assert result.rows[0]["distance_mi"] == 5.0
    assert result.rows[0]["duration_sec"] == 2700


def test_timestamp_column_is_not_mistaken_for_duration():
    mapping, _ = match_columns(["Date", "Timestamp", "Distance", "Duration"])
    assert mapping["duration_sec"] == "Duration"
