"""Taking a run out, and keeping it out.

Deleting a row is not enough on its own. A run's identity comes from the sources
that produced it -- a spreadsheet row that is still in the CSV, a workout that is
still in the Health export -- so the next import simply recreates it. Recording
the exclusion is what makes the removal hold.

    python -m ingest.exclude 3 --reason "GPS glitch"
    python -m ingest.exclude --list
    python -m ingest.exclude --restore 3
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone

from .schema import connect, init_db


def excluded_keys(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
    """(run_keys, health_ids) that importers should skip."""
    rows = conn.execute("SELECT run_key, health_id FROM excluded_runs").fetchall()
    return ({r["run_key"] for r in rows if r["run_key"]},
            {r["health_id"] for r in rows if r["health_id"]})


def exclude(conn: sqlite3.Connection, run_id: int, reason: str | None = None) -> dict:
    """Remove a run and remember that it should stay removed."""
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise LookupError(f"no run #{run_id}")

    conn.execute(
        "INSERT INTO excluded_runs (run_key, health_id, date, reason, excluded_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (run["run_key"], run["health_id"], run["date"], reason,
         datetime.now(timezone.utc).isoformat(timespec="seconds")))
    # Explicit rather than relying on cascade: the web app opens its connection
    # without foreign keys switched on.
    conn.execute("DELETE FROM hr_samples WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM route_points WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    conn.commit()
    return dict(run)


def restore(conn: sqlite3.Connection, run_key: str) -> int:
    """Stop excluding something. The next import brings the run back."""
    removed = conn.execute(
        "DELETE FROM excluded_runs WHERE run_key = ? OR health_id = ?",
        (run_key, run_key)).rowcount
    conn.commit()
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Exclude a run from the database.")
    parser.add_argument("run_id", nargs="?", type=int)
    parser.add_argument("--db", default="data/runs.db")
    parser.add_argument("--reason", default=None)
    parser.add_argument("--list", action="store_true", help="Show what is excluded.")
    parser.add_argument("--restore", metavar="RUN_KEY",
                        help="Un-exclude a run key or health id.")
    args = parser.parse_args(argv)

    conn = connect(args.db)
    init_db(conn)
    try:
        if args.list:
            rows = conn.execute(
                "SELECT * FROM excluded_runs ORDER BY date").fetchall()
            if not rows:
                print("Nothing is excluded.")
            for r in rows:
                print(f"  {r['date']}  {r['run_key']}"
                      + (f"  ({r['reason']})" if r["reason"] else ""))
            return 0

        if args.restore:
            count = restore(conn, args.restore)
            print(f"Removed {count} exclusion(s). The next `make ingest` restores the run.")
            return 0

        if args.run_id is None:
            parser.error("give a run id, --list, or --restore")

        run = exclude(conn, args.run_id, args.reason)
        print(f"Removed run #{args.run_id}: {run['date']} · "
              f"{run['distance_mi']} mi · {run['notes'] or ''}")
        print("Recorded as excluded, so re-importing will not bring it back.")
        return 0
    except LookupError as problem:
        print(problem, file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
