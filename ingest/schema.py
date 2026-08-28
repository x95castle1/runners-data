"""The canonical `runs` table, and the header-matching that fills it.

Every source -- the training spreadsheet today, Apple Health later -- normalizes
into this one table. Adding a source means writing a loader that emits these
field names; nothing downstream changes.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_key         TEXT    NOT NULL UNIQUE,
    source          TEXT    NOT NULL,
    date            TEXT    NOT NULL,          -- ISO YYYY-MM-DD
    status          TEXT    NOT NULL DEFAULT 'completed',  -- completed | planned | skipped
    week_number     INTEGER,
    distance_mi     REAL,
    duration_sec    INTEGER,
    pace_sec_per_mi REAL,
    avg_hr          INTEGER,
    max_hr          INTEGER,
    elevation_ft    REAL,
    cadence         INTEGER,
    steps           INTEGER,
    calories        INTEGER,
    workout_type    TEXT,
    effort          TEXT,
    route           TEXT,
    weather         TEXT,
    shoes           TEXT,
    notes           TEXT,
    extra           TEXT,                      -- JSON: columns we didn't map
    logged_at       TEXT,                      -- set when a time was entered in the app
    prior_distance_mi REAL,                    -- what the plan said, so Undo can restore it
    -- Filled in by the Apple Health loader.
    started_at      TEXT,                      -- local timestamp; two-a-days, ordering
    health_id       TEXT,                      -- HealthKit workout UUID
    measured_source TEXT,                      -- who supplied the measurements
    source_name     TEXT,                      -- 'Apple Watch', 'Nike Run Club', ...
    min_hr          INTEGER,
    temperature_f   REAL,
    humidity_pct    REAL,
    route_points    INTEGER,                   -- point count, so the UI can skip a join
    weather_place   TEXT,                      -- which weather_hours cell this run sits in
    vo2_max         REAL,                      -- the Watch's estimate current on this run
    vo2_max_date    TEXT,                      -- when it was measured; may predate the run
    imported_at     TEXT    NOT NULL
);

-- One row per week of the training plan, straight from the spreadsheet. Keeps
-- the runner's own weekly totals (which don't always match the sum of the day
-- cells) and anything tracked weekly rather than per-run, like body weight.
CREATE TABLE IF NOT EXISTS weeks (
    week_key       TEXT PRIMARY KEY,
    source         TEXT    NOT NULL,
    week_number    INTEGER,
    week_start     TEXT    NOT NULL,
    sheet_miles    REAL,
    sheet_seconds  INTEGER,
    weight_lb      REAL,
    notes          TEXT,
    imported_at    TEXT    NOT NULL
);

-- Per-run time series. Kept in two tables rather than one because heart rate and
-- GPS are sampled at different rates -- a merged table would be mostly NULL.
CREATE TABLE IF NOT EXISTS hr_samples (
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    offset_sec INTEGER NOT NULL,
    bpm        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS route_points (
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    offset_sec  INTEGER NOT NULL,
    lat         REAL    NOT NULL,
    lon         REAL    NOT NULL,
    altitude_ft REAL,
    speed_mph   REAL
);

-- Hourly weather, cached by place and hour rather than by run, so two runs on
-- the same day in the same neighbourhood cost one lookup between them. Splits
-- interpolate between these rows at read time; nothing per-split is stored, so
-- the split logic can change without re-fetching anything.
CREATE TABLE IF NOT EXISTS weather_hours (
    place        TEXT NOT NULL,      -- "40.50,-88.95", rounded to about a km
    hour_ts      TEXT NOT NULL,      -- local time, ISO
    temp_f       REAL,
    apparent_f   REAL,
    humidity_pct REAL,
    dew_point_f  REAL,
    wind_mph     REAL,
    wind_dir_deg REAL,
    precip_in    REAL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (place, hour_ts)
);

-- Runs deliberately taken out. Both importers consult this, so a row removed
-- here does not quietly reappear on the next import -- which it otherwise would,
-- since its identity comes from the spreadsheet and the Health export that keep
-- producing it.
-- Health metrics that describe a day rather than a workout: VO2 max today, and
-- room for HRV or resting heart rate later without another schema change.
-- Stretches where the watch was paused. Apple's `duration` excludes them but
-- the samples and GPS points span them, so anything measured off sample
-- timestamps has to subtract these or it counts standing still as running.
-- Steps per bucket of a run. Stored raw rather than as a rate so the paused
-- seconds can be taken out of the denominator when it is read.
CREATE TABLE IF NOT EXISTS cadence_samples (
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    offset_sec INTEGER NOT NULL,
    steps      REAL    NOT NULL,
    span_sec   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS run_pauses (
    run_id    INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    start_sec INTEGER NOT NULL,
    end_sec   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS health_metrics (
    metric      TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    date        TEXT NOT NULL,
    value       REAL NOT NULL,
    unit        TEXT,
    source_name TEXT,
    PRIMARY KEY (metric, recorded_at)
);

CREATE TABLE IF NOT EXISTS excluded_runs (
    run_key     TEXT,
    health_id   TEXT,
    date        TEXT,
    reason      TEXT,
    excluded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    filename    TEXT NOT NULL,
    rows_read   INTEGER NOT NULL,
    rows_kept   INTEGER NOT NULL,
    imported_at TEXT NOT NULL,
    mapping     TEXT                            -- JSON: header -> canonical field
);
"""


INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_runs_date ON runs(date);

CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE INDEX IF NOT EXISTS idx_runs_type ON runs(workout_type);

-- Partial index: a run key is unique per source, but health_id is unique globally
-- and most rows don't have one.
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_health_id
    ON runs(health_id) WHERE health_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_hr_run ON hr_samples(run_id, offset_sec);

CREATE INDEX IF NOT EXISTS idx_route_run ON route_points(run_id, offset_sec);

CREATE INDEX IF NOT EXISTS idx_cadence_run ON cadence_samples(run_id, offset_sec);
CREATE INDEX IF NOT EXISTS idx_pause_run ON run_pauses(run_id, start_sec);
CREATE INDEX IF NOT EXISTS idx_metric_date ON health_metrics(metric, date);
CREATE INDEX IF NOT EXISTS idx_excluded_key ON excluded_runs(run_key);
CREATE INDEX IF NOT EXISTS idx_excluded_health ON excluded_runs(health_id);
"""

# Fields a loader may emit. Order is the display order in the UI.
FIELDS = [
    "date",
    "status",
    "week_number",
    "distance_mi",
    "duration_sec",
    "pace_sec_per_mi",
    "avg_hr",
    "max_hr",
    "elevation_ft",
    "cadence",
    "calories",
    "workout_type",
    "effort",
    "route",
    "weather",
    "shoes",
    "notes",
]

# Spreadsheet headers we recognize, most-specific alias first. Matching is done
# on a squashed form of the header ("Avg HR (bpm)" -> "avghrbpm"), first by exact
# hit and then by containment, so unseen variants usually still land.
ALIASES: dict[str, list[str]] = {
    "date":            ["date", "rundate", "workoutdate", "day", "when"],
    "distance_mi":     ["distancemi", "distancekm", "distance", "miles", "mileage",
                        "milesrun", "dist", "km", "kilometers", "length"],
    "duration_sec":    ["duration", "movingtime", "elapsedtime", "totaltime",
                        "runtime", "time", "durationhhmmss"],
    "pace_sec_per_mi": ["avgpace", "averagepace", "pacepermile", "paceminmi",
                        "pacepermi", "minmi", "minpermile", "pace"],
    "avg_hr":          ["avghr", "averagehr", "avgheartrate", "averageheartrate",
                        "heartrate", "avgbpm", "hr", "bpm"],
    "max_hr":          ["maxhr", "maxheartrate", "peakhr", "maxbpm"],
    "elevation_ft":    ["elevationgain", "elevgain", "elevation", "ascent",
                        "climb", "vert", "gain"],
    "cadence":         ["avgcadence", "cadence", "spm", "stepsperminute"],
    "calories":        ["calories", "kcal", "cal", "energy"],
    "workout_type":    ["workouttype", "runtype", "sessiontype", "type", "workout",
                        "session", "category", "kind"],
    "effort":          ["perceivedeffort", "rpe", "effort", "feel", "howitfelt",
                        "intensity"],
    "route":           ["route", "location", "where", "course", "trail"],
    "weather":         ["weather", "temperature", "temp", "conditions"],
    "shoes":           ["shoes", "shoe", "footwear", "gear"],
    "notes":           ["notes", "note", "comments", "comment", "description",
                        "remarks", "howdiditgo", "details"],
}

# Headers that look like an alias but aren't. Checked before containment.
NEVER = {
    "duration_sec": {"timestamp", "timeofday", "starttime", "endtime", "sleeptime"},
    "distance_mi":  {"distancetogo", "totaldistance", "cumulativemiles",
                     "weeklymiles", "plannedmiles", "targetmiles", "goalmiles"},
    "date":         {"dateadded", "updatedate", "createdate"},
}

def squash(header: str) -> str:
    """'Avg HR (bpm)' -> 'avghrbpm'. Makes header matching forgiving."""
    return re.sub(r"[^a-z0-9]", "", str(header).lower())

def unit_hint(header: str, field: str) -> str:
    """Read the unit out of the header text, since the cells rarely carry one."""
    squashed = squash(header)
    if field == "elevation_ft":
        return "m" if re.search(r"(meters?|metres?|\bm\b)", str(header), re.I) else "ft"
    if "km" in squashed or "kilom" in squashed:
        return "km"
    return "mi"

def match_columns(headers: list[str]) -> tuple[dict[str, str], list[str]]:
    """Map canonical field -> source header. Returns (mapping, unmapped headers)."""
    mapping: dict[str, str] = {}
    claimed: set[str] = set()

    def try_claim(field: str, header: str) -> bool:
        if field in mapping or header in claimed:
            return False
        if squash(header) in NEVER.get(field, set()):
            return False
        mapping[field] = header
        claimed.add(header)
        return True

    # Pass 1: exact match on the squashed header. Highest confidence.
    for field, aliases in ALIASES.items():
        for alias in aliases:
            hit = next((h for h in headers if squash(h) == alias), None)
            if hit and try_claim(field, hit):
                break

    # Pass 2: containment, longest alias first so "avgheartrate" beats "hr".
    for field, aliases in ALIASES.items():
        if field in mapping:
            continue
        for alias in sorted(aliases, key=len, reverse=True):
            hit = next((h for h in headers if alias in squash(h)), None)
            if hit and try_claim(field, hit):
                break

    unmapped = [h for h in headers if h and h not in claimed]
    return mapping, unmapped

def load_overrides(path: str | Path = "config/columns.json") -> dict[str, str]:
    """Optional hand-written mapping for headers the matcher gets wrong.

    Shape: {"canonical_field": "Exact Sheet Header", ...}
    An empty string unmaps a field entirely.
    """
    path = Path(path)
    if not path.exists():
        return {}
    with path.open() as fh:
        return json.load(fh)

def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

# Columns added after the first release. Applied to existing databases on open so
# an old runs.db keeps working without a manual rebuild.
MIGRATIONS = [
    ("runs", "status", "TEXT NOT NULL DEFAULT 'completed'"),
    ("runs", "week_number", "INTEGER"),
    ("runs", "logged_at", "TEXT"),
    ("runs", "prior_distance_mi", "REAL"),
    ("runs", "started_at", "TEXT"),
    ("runs", "health_id", "TEXT"),
    ("runs", "measured_source", "TEXT"),
    ("runs", "source_name", "TEXT"),
    ("runs", "min_hr", "INTEGER"),
    ("runs", "temperature_f", "REAL"),
    ("runs", "humidity_pct", "REAL"),
    ("runs", "route_points", "INTEGER"),
    ("runs", "weather_place", "TEXT"),
    ("runs", "vo2_max", "REAL"),
    ("runs", "vo2_max_date", "TEXT"),
    ("runs", "steps", "INTEGER"),
]

def init_db(conn: sqlite3.Connection) -> None:
    # Tables first, then migrations, then indexes -- an index may reference a
    # column that only exists on an older database once the ALTER has run.
    conn.executescript(SCHEMA_SQL)
    for table, column, decl in MIGRATIONS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.executescript(INDEX_SQL)
    conn.commit()
