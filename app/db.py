"""Database access. Read-only as far as the web app is concerned -- writes only
ever happen through the ingest CLI, so the app can't corrupt your training log.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from ingest.schema import init_db

DB_PATH = Path(os.environ.get("RUNS_DB", "data/runs.db"))


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Creating the schema on read means a fresh checkout renders the empty
    # state instead of throwing "no such table".
    init_db(conn)
    return conn


def has_data(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] > 0
