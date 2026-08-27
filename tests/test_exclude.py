"""Removing a run, and it staying removed.

The point of these is the second half. Deleting the row is easy; what matters is
that the spreadsheet row and the Health workout that produced it don't quietly
put it back on the next import.
"""

import pytest

from ingest.cli import RUN_COLUMNS, run_upsert
from ingest.exclude import exclude, excluded_keys, restore
from ingest.schema import connect, init_db


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "e.db")
    init_db(c)
    c.execute(
        "INSERT INTO runs (run_key, source, date, status, distance_mi, duration_sec,"
        " health_id, measured_source, notes, imported_at) VALUES"
        " ('sheet-plan:2026-01-08:thursday', 'sheet-plan', '2026-01-08', 'completed',"
        "  1.471, 1801, 'start:2026-01-08T17:51:51-05:00', 'apple-health',"
        "  'Nike 1,2,3 Go', 'x')")
    run_id = c.execute("SELECT id FROM runs").fetchone()["id"]
    c.executemany("INSERT INTO hr_samples (run_id, offset_sec, bpm) VALUES (?, ?, ?)",
                  [(run_id, i * 30, 120) for i in range(20)])
    c.executemany(
        "INSERT INTO route_points (run_id, offset_sec, lat, lon, altitude_ft, speed_mph)"
        " VALUES (?, ?, 40.5, -88.95, 700, 5)", [(run_id, i * 10) for i in range(50)])
    c.commit()
    return c


def test_the_run_and_its_series_are_removed(conn):
    exclude(conn, 1, reason="GPS outlier")
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM hr_samples").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM route_points").fetchone()[0] == 0


def test_both_identities_are_remembered(conn):
    """A merged run has two: the plan's row key and the workout's id. Miss either
    and the other source recreates it."""
    exclude(conn, 1)
    keys, health = excluded_keys(conn)
    assert "sheet-plan:2026-01-08:thursday" in keys
    assert "start:2026-01-08T17:51:51-05:00" in health


def test_the_reason_is_kept(conn):
    exclude(conn, 1, reason="GPS outlier")
    assert conn.execute(
        "SELECT reason FROM excluded_runs").fetchone()["reason"] == "GPS outlier"


def test_excluding_something_that_isnt_there(conn):
    with pytest.raises(LookupError):
        exclude(conn, 999)


def test_a_spreadsheet_reimport_does_not_bring_it_back(conn):
    exclude(conn, 1)
    keys, _ = excluded_keys(conn)

    incoming = {c: None for c in RUN_COLUMNS}
    incoming.update(run_key="sheet-plan:2026-01-08:thursday", source="sheet-plan",
                    date="2026-01-08", status="completed", distance_mi=1.5,
                    notes="Nike 1,2,3 Go", imported_at="later")
    # What the importer does: drop excluded keys before writing.
    rows = [r for r in [incoming] if r["run_key"] not in keys]
    assert rows == []
    conn.executemany(run_upsert(), rows)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_a_health_reimport_does_not_bring_it_back(conn):
    exclude(conn, 1)
    _, health = excluded_keys(conn)
    workouts = [{"health_id": "start:2026-01-08T17:51:51-05:00", "date": "2026-01-08"}]
    assert [w for w in workouts if w["health_id"] not in health] == []


def test_restore_lets_it_come_back(conn):
    exclude(conn, 1)
    assert restore(conn, "sheet-plan:2026-01-08:thursday") == 1
    keys, health = excluded_keys(conn)
    assert not keys and not health


def test_other_runs_are_untouched(conn):
    conn.execute(
        "INSERT INTO runs (run_key, source, date, status, distance_mi, imported_at)"
        " VALUES ('sheet-plan:2026-01-10:saturday', 'sheet-plan', '2026-01-10',"
        " 'completed', 4.0, 'x')")
    conn.commit()
    exclude(conn, 1)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    keys, _ = excluded_keys(conn)
    assert "sheet-plan:2026-01-10:saturday" not in keys
