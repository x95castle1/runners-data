from datetime import date

import pytest

from ingest import normalize as nz


@pytest.mark.parametrize("value,expected", [
    ("1:23:45", 5025),
    ("45:30", 2730),
    ("0:45:00", 2700),
    ("45", 2700),          # bare number = minutes
    ("45 min", 2700),
    ("1h 5m", 3900),
    ("1h 5m 30s", 3930),
    ("90s", 90),
    ("", None),
    ("-", None),
    ("garbage", None),
])
def test_parse_duration(value, expected):
    assert nz.parse_duration(value) == expected


@pytest.mark.parametrize("value,hint,expected", [
    ("6.2", "mi", 6.2),
    ("6.2 mi", "mi", 6.2),
    ("6.2 miles", "mi", 6.2),
    ("10 km", "mi", 6.2137),
    ("10k", "mi", 6.2137),
    ("10", "km", 6.2137),        # unit comes from the column header
    ("5000 m", "mi", 3.1069),
    ("6.2 m", "mi", 6.2),        # a run log's "m" means miles, not metres
])
def test_parse_distance(value, hint, expected):
    assert nz.parse_distance(value, unit_hint=hint) == pytest.approx(expected, abs=0.001)


@pytest.mark.parametrize("value,hint,expected", [
    ("8:42", "mi", 522),
    ("8:42/mi", "mi", 522),
    ("5:23/km", "mi", 519.8),    # km pace converts to per-mile
    ("5:23", "km", 519.8),
])
def test_parse_pace(value, hint, expected):
    assert nz.parse_pace(value, unit_hint=hint) == pytest.approx(expected, abs=0.1)


@pytest.mark.parametrize("value", [
    "2026-06-01", "6/1/2026", "Jun 1, 2026", "Mon 6/1/2026", "Monday, June 1 2026",
])
def test_parse_date(value):
    assert nz.parse_date(value) == date(2026, 6, 1)


def test_parse_date_excel_serial():
    assert nz.parse_date("46174") == date(2026, 6, 1)


def test_parse_elevation_units():
    assert nz.parse_elevation("500", unit_hint="ft") == 500
    assert nz.parse_elevation("100 m", unit_hint="ft") == pytest.approx(328.08, abs=0.1)


def test_formatting():
    assert nz.format_duration(5025) == "1:23:45"
    assert nz.format_duration(2730) == "45:30"
    assert nz.format_pace(522) == "8:42"
    assert nz.format_pace(None) == "-"
