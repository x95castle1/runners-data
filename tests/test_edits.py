"""Logging a run from the app, and what a later re-ingest does to it."""

import sqlite3

import pytest

from app import edits
from ingest.cli import RUN_COLUMNS, run_upsert
from ingest.schema import connect, init_db


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    c.executemany(
        "INSERT INTO runs (run_key, source, date, status, distance_mi, workout_type,"
        " notes, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?, '2026-08-24')",
        [("sheet-plan:2026-08-29:saturday", "sheet-plan", "2026-08-29", "planned",
          20.0, "Long", "20 Miles"),
         ("sheet-plan:2026-08-25:tuesday", "sheet-plan", "2026-08-25", "planned",
          9.0, "Speed", "Yasso 800's")])
    c.commit()
    return c


def run_id(conn, date):
    return conn.execute("SELECT id FROM runs WHERE date = ?", (date,)).fetchone()["id"]


def get(conn, date):
    return conn.execute("SELECT * FROM runs WHERE date = ?", (date,)).fetchone()


# --- parsing ----------------------------------------------------------------

@pytest.mark.parametrize("text,seconds", [
    ("48:12", 2892), ("1:02:30", 3750), ("48", 2880), ("3:22:24", 12144),
])
def test_times_people_actually_type(text, seconds):
    assert edits.parse_entry(text, "")[0] == seconds


@pytest.mark.parametrize("text", ["", "   ", "abc", "0", "0:00"])
def test_unusable_times_are_rejected(text):
    with pytest.raises(edits.InvalidEntry):
        edits.parse_entry(text, "")


def test_absurd_values_are_rejected():
    with pytest.raises(edits.InvalidEntry, match="24 hours"):
        edits.parse_entry("25:00:00", "")
    with pytest.raises(edits.InvalidEntry, match="100 miles"):
        edits.parse_entry("48:12", "150")


def test_blank_distance_is_allowed():
    assert edits.parse_entry("48:12", "") == (2892, None)


# --- logging ----------------------------------------------------------------

def test_logging_completes_the_run_and_computes_pace(conn):
    edits.log_run(conn, run_id(conn, "2026-08-29"), duration_text="3:22:24")
    row = get(conn, "2026-08-29")
    assert row["status"] == "completed"
    assert row["duration_sec"] == 12144
    assert row["distance_mi"] == 20.0          # kept the planned distance
    assert row["pace_sec_per_mi"] == pytest.approx(607.2)
    assert row["logged_at"]


def test_logging_can_override_the_planned_distance(conn):
    edits.log_run(conn, run_id(conn, "2026-08-29"), duration_text="2:30:00",
                  distance_text="15.2")
    row = get(conn, "2026-08-29")
    assert row["distance_mi"] == 15.2
    assert row["pace_sec_per_mi"] == pytest.approx(9000 / 15.2)


def test_a_bad_time_changes_nothing(conn):
    with pytest.raises(edits.InvalidEntry):
        edits.log_run(conn, run_id(conn, "2026-08-29"), duration_text="nope")
    assert get(conn, "2026-08-29")["status"] == "planned"


def test_undo_restores_the_planned_distance(conn):
    rid = run_id(conn, "2026-08-29")
    edits.log_run(conn, rid, duration_text="2:30:00", distance_text="15.2")
    assert get(conn, "2026-08-29")["distance_mi"] == 15.2
    edits.unlog(conn, rid)
    row = get(conn, "2026-08-29")
    assert row["distance_mi"] == 20.0            # back to what the plan said
    assert row["prior_distance_mi"] is None


def test_skip_and_undo(conn):
    rid = run_id(conn, "2026-08-25")
    edits.mark_skipped(conn, rid)
    assert get(conn, "2026-08-25")["status"] == "skipped"
    edits.unlog(conn, rid)
    row = get(conn, "2026-08-25")
    assert row["status"] == "planned"
    assert row["duration_sec"] is None
    assert row["logged_at"] is None


# --- what a re-ingest does to it --------------------------------------------

def reingest(conn, **overrides):
    """Re-import the same run as the spreadsheet would present it."""
    row = {c: None for c in RUN_COLUMNS}
    row.update(run_key="sheet-plan:2026-08-29:saturday", source="sheet-plan",
               date="2026-08-29", status="planned", distance_mi=20.0,
               workout_type="Long", notes="20 Miles", imported_at="2026-09-01")
    row.update(overrides)
    conn.execute(run_upsert(), row)
    conn.commit()


def test_reimport_keeps_a_logged_time_the_sheet_still_lacks(conn):
    edits.log_run(conn, run_id(conn, "2026-08-29"), duration_text="3:22:24")
    reingest(conn)                                  # sheet still says planned
    row = get(conn, "2026-08-29")
    assert row["status"] == "completed"
    assert row["duration_sec"] == 12144
    assert row["logged_at"]


def test_the_sheet_takes_the_run_back_once_it_has_a_time(conn):
    edits.log_run(conn, run_id(conn, "2026-08-29"), duration_text="3:22:24")
    reingest(conn, status="completed", duration_sec=12000,
             pace_sec_per_mi=600.0, notes="20 Miles")
    row = get(conn, "2026-08-29")
    assert row["duration_sec"] == 12000           # the sheet's value wins
    assert row["logged_at"] is None               # no longer an app entry


def test_reimport_still_refreshes_untouched_fields(conn):
    edits.log_run(conn, run_id(conn, "2026-08-29"), duration_text="3:22:24")
    reingest(conn, workout_type="Race", week_number=34)
    row = get(conn, "2026-08-29")
    assert row["workout_type"] == "Race"          # not a logged field
    assert row["week_number"] == 34
    assert row["duration_sec"] == 12144           # logged field still protected


def test_untouched_runs_are_overwritten_as_before(conn):
    reingest(conn, status="completed", duration_sec=11000, pace_sec_per_mi=550.0)
    assert get(conn, "2026-08-29")["duration_sec"] == 11000
