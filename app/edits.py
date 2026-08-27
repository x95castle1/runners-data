"""The one place the web app writes.

Everything else reads. A run edited here is stamped with `logged_at`, which is
what tells a later `make ingest` to leave it alone -- until the spreadsheet
itself has a time for that run, at which point the sheet takes it back.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ingest import normalize as nz


class InvalidEntry(ValueError):
    """Something the runner typed didn't parse. The message is shown to them."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_entry(duration_text: str, distance_text: str) -> tuple[int, float | None]:
    """Validate what came off the form, with messages worth reading."""
    duration = nz.parse_duration(duration_text)
    if duration is None:
        raise InvalidEntry(
            f"Couldn't read {duration_text.strip()!r} as a time. "
            "Try 48:12, 1:02:30, or 48 for minutes."
        )
    if duration <= 0:
        raise InvalidEntry("A completed run needs a time greater than zero.")
    if duration > 24 * 3600:
        raise InvalidEntry("That's over 24 hours — check the time you entered.")

    distance = None
    if str(distance_text or "").strip():
        distance = nz.parse_distance(distance_text)
        if distance is None:
            raise InvalidEntry(f"Couldn't read {distance_text.strip()!r} as a distance.")
        if distance <= 0:
            raise InvalidEntry("Distance has to be greater than zero.")
        if distance > 100:
            raise InvalidEntry("Over 100 miles — check the distance you entered.")
    return duration, distance


def log_run(conn: sqlite3.Connection, run_id: int, *, duration_text: str,
            distance_text: str = "", notes: str | None = None) -> dict:
    """Record a time against an existing run and mark it completed."""
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise InvalidEntry(f"No run #{run_id}.")

    duration, distance = parse_entry(duration_text, distance_text)
    distance = distance if distance is not None else run["distance_mi"]
    pace = (duration / distance) if distance else None

    conn.execute(
        "UPDATE runs SET status = 'completed', duration_sec = ?, distance_mi = ?,"
        " pace_sec_per_mi = ?, notes = COALESCE(?, notes), logged_at = ?,"
        # Remember what the plan called for the first time we overwrite it, so
        # Undo can put it back.
        " prior_distance_mi = COALESCE(prior_distance_mi, distance_mi)"
        " WHERE id = ?",
        (duration, distance, pace, notes or None, _now(), run_id),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())


def mark_skipped(conn: sqlite3.Connection, run_id: int) -> None:
    """It was on the plan and didn't happen."""
    conn.execute(
        "UPDATE runs SET status = 'skipped', duration_sec = NULL,"
        " pace_sec_per_mi = NULL, logged_at = ? WHERE id = ?",
        (_now(), run_id),
    )
    conn.commit()


def unlog(conn: sqlite3.Connection, run_id: int) -> None:
    """Undo: put the run back on the plan as if nothing had been entered."""
    conn.execute(
        "UPDATE runs SET status = 'planned', duration_sec = NULL,"
        " pace_sec_per_mi = NULL, logged_at = NULL,"
        " distance_mi = COALESCE(prior_distance_mi, distance_mi),"
        " prior_distance_mi = NULL WHERE id = ?",
        (run_id,),
    )
    conn.commit()
