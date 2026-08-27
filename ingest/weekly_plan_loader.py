"""Adapter for a week-per-row training plan.

Shape: one row per training week, one column per weekday, and a `Time` column
following each day that is actually run. Weekly totals and body weight sit at
the end of the row:

    Week | Date | Monday | Time | Tuesday | Time | Wednesday | ... | Weight | Total Miles | Total Time

The day cell holds the workout as written -- "4 Miles Easy", "Hill Repeats
(8 Miles with 8 Repeats)", "Rest" -- so the distance has to be read out of prose.
Three quirks of this export, each verified against the sheet's own totals:

* Day times export as MM:SS with a junk third component (`30:00:00` is thirty
  minutes, not thirty hours) because they were typed into duration-formatted
  cells. Weekly `Total Time` is a normal H:MM:SS.
* A day with a `Time` of `0` or `0:00:00` was planned but not run.
* A `Time` column belongs to the *block* of weekday columns before it, not to
  the single day beside it. When a workout moves -- a long run done Friday, a
  race on Sunday -- the text moves to the day it happened and the time stays in
  the block's fixed slot. So each time is matched to the last non-rest day in
  its block, falling forward to a trailing day column when the whole block is
  rest. This is what makes the weekly totals reconcile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import normalize as nz

SOURCE = "sheet-plan"

WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6}

# Cells that record an absence rather than a run.
NON_RUN = re.compile(r"^\s*(rest|vacation|n/?a|off|none|-+)\b", re.I)

# Named races, which carry a standard distance the cell text doesn't state.
RACE_DISTANCES = {
    "marathon": 26.2,
    "half marathon": 13.1,
    "10k": 6.2137,
    "5k": 3.1069,
}

# How the workout was written -> a type worth filtering and coloring by. First
# match wins, so the specific patterns come before the general ones.
TYPE_RULES = [
    ("Race",         r"\bmarathon\b|\bhalf\b|\b10k\b|\b5k\b|shuffle|\brace\b"),
    ("Speed",        r"repeat|yasso|interval|\d+\s*x\s*\d|pyramid|4x4|sneaky speed|"
                     r"run fast|speed|fartlek|\b800'?s?\b"),
    ("Marathon pace", r"@\s*mp\b|marathon pace"),
    ("Tempo",        r"tempo|threshold|one hard"),
    ("Long",         r"\blong\b"),
    ("Easy",         r"\beasy\b|recovery|shakeout"),
]

DISTANCE_PATTERNS = [
    r"^(\d+(?:\.\d+)?)\s*mi(?:le)?s?\b",        # "10 Miles Easy"
    r"^(\d+(?:\.\d+)?)\s*-",                     # "13.1 - Cham Half"
    r"\(\s*(\d+(?:\.\d+)?)\s*mi(?:le)?s?\b",     # "Hill Repeats (8 Miles with 8 Repeats)"
    r"\(\s*(\d+(?:\.\d+)?)\s*\)",                # "Power Pyramid (2.8)"
    r"(\d+(?:\.\d+)?)\s*mi(?:le)?s?\b",          # "Shamrock Shuffle 5 Miles"
]


@dataclass
class PlanResult:
    rows: list[dict] = field(default_factory=list)
    weeks: list[dict] = field(default_factory=list)
    blocks: list[tuple[list[tuple[str, int]], int]] = field(default_factory=list)
    trailing_days: list[tuple[str, int]] = field(default_factory=list)
    header_row: int = 0
    rows_read: int = 0
    completed: int = 0
    planned: int = 0
    skipped: int = 0
    derived_distances: list[str] = field(default_factory=list)
    discrepancies: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def looks_like_weekly_plan(headers: list[str]) -> bool:
    """A plan row is recognizable by three or more weekday columns."""
    squashed = [h.strip().lower() for h in headers]
    return sum(1 for h in squashed if h in WEEKDAYS) >= 3


def day_blocks(headers: list[str]) -> tuple[list[tuple[list[tuple[str, int]], int]], list[tuple[str, int]]]:
    """Split the weekday columns into (days, time column) blocks.

    Returns the blocks plus any trailing day columns that follow the last Time
    column -- the fall-forward candidates for a workout that slipped a day.
    """
    blocks: list[tuple[list[tuple[str, int]], int]] = []
    pending: list[tuple[str, int]] = []
    for i, header in enumerate(headers):
        name = header.strip().lower()
        if name in WEEKDAYS:
            pending.append((header.strip(), i))
        elif name == "time":
            blocks.append((pending, i))
            pending = []
    return blocks, pending


def parse_day_time(value: str) -> int | None:
    """MM:SS, ignoring the third component the export tacks on.

    `0` and `0:00:00` mean the run was planned but not done -- reported as 0 so
    the caller can tell "didn't happen" from "no time recorded".
    """
    text = str(value or "").strip()
    if not text:
        return None
    parts = [p for p in text.split(":") if p != ""]
    try:
        if len(parts) == 1:
            return int(float(parts[0])) or 0
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return None


def parse_total_time(value: str) -> int | None:
    """The weekly total is an ordinary H:MM:SS."""
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return None


def parse_workout_distance(text: str) -> float | None:
    """Miles, read out of the workout description."""
    value = str(text or "").strip()
    if not value or NON_RUN.match(value):
        return None
    lowered = value.lower()
    if not re.search(r"\d", lowered):
        # Longest name first, so "half marathon" wins over "marathon".
        for name in sorted(RACE_DISTANCES, key=len, reverse=True):
            if name in lowered:
                return RACE_DISTANCES[name]
    for pattern in DISTANCE_PATTERNS:
        match = re.search(pattern, value, re.I)
        if match:
            return float(match.group(1))
    return None


def classify(text: str, *, distance: float | None) -> str:
    """A workout type the sheet doesn't state, read out of how it was written.

    The raw cell text is kept in `notes` either way, so nothing here overwrites
    what the runner actually recorded.
    """
    value = str(text or "")
    for label, pattern in TYPE_RULES:
        if re.search(pattern, value, re.I):
            return label
    if distance and distance >= 10:
        return "Long"
    return "Run"


def load_weekly_plan(path: str | Path) -> PlanResult:
    import csv

    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        grid = [row for row in csv.reader(fh)]

    result = PlanResult()
    header_index = next(
        (i for i, row in enumerate(grid[:10]) if looks_like_weekly_plan(row)), None
    )
    if header_index is None:
        result.warnings.append(f"{path.name}: no weekday columns found")
        return result

    headers = [h.strip() for h in grid[header_index]]
    result.header_row = header_index
    result.blocks, result.trailing_days = day_blocks(headers)

    def column(name_options: list[str]) -> int | None:
        for i, header in enumerate(headers):
            if header.strip().lower() in name_options:
                return i
        return None

    col_date = column(["date", "week of", "week start"])
    col_week = column(["week", "week #", "wk"])
    col_weight = column(["weight", "weight (lb)", "wt"])
    col_miles = column(["total miles", "miles", "weekly miles"])
    col_time = column(["total time", "time total", "weekly time"])

    if col_date is None:
        result.warnings.append(f"{path.name}: no Date column on the plan rows")
        return result

    imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    last_block_index = len(result.blocks) - 1

    for raw in grid[header_index + 1:]:
        def cell(i: int | None) -> str:
            return raw[i].strip() if i is not None and i < len(raw) else ""

        week_start = nz.parse_date(cell(col_date))
        if week_start is None:
            continue  # trailer rows carrying grand totals
        result.rows_read += 1

        week_number = nz.parse_int(cell(col_week))
        sheet_miles = nz.parse_number(cell(col_miles))
        sheet_seconds = parse_total_time(cell(col_time))
        weight = nz.parse_number(cell(col_weight))
        week_key = f"{SOURCE}:{week_start.isoformat()}"

        annotations, week_runs = [], []
        used_columns: set[int] = set()

        for block_index, (days, time_col) in enumerate(result.blocks):
            candidates = [(name, col, cell(col)) for name, col in days]
            runnable = [c for c in candidates if c[2] and not NON_RUN.match(c[2])]

            # The whole block is rest, so a workout in this slot slipped past the
            # last day column -- week 11's Sunday race, for instance.
            if not runnable and block_index == last_block_index:
                runnable = [(name, col, cell(col)) for name, col in result.trailing_days
                            if cell(col) and not NON_RUN.match(cell(col))]

            if not runnable:
                continue

            # One time, one run: the last non-rest day in the block owns it.
            name, col, text = runnable[-1]
            for other_name, other_col, other_text in runnable[:-1]:
                annotations.append(f"{other_name}: {other_text}")
                result.warnings.append(
                    f"week {week_number}: {other_name} '{other_text}' shares a Time "
                    f"column with {name}; imported without a duration"
                )
                used_columns.add(other_col)
                week_runs.append({"date": (week_start + timedelta(
                    days=WEEKDAYS[other_name.lower()])).isoformat(),
                    "text": other_text, "duration_sec": None,
                    "status": "planned", "day": other_name})

            used_columns.add(col)
            seconds = parse_day_time(cell(time_col))
            if seconds:
                status = "completed"
            elif seconds == 0:
                # An explicit zero: it was on the plan and didn't happen.
                status = "skipped"
            else:
                status = "planned"
            week_runs.append({
                "date": (week_start + timedelta(days=WEEKDAYS[name.lower()])).isoformat(),
                "text": text, "duration_sec": seconds or None,
                "status": status, "day": name,
            })

        # Anything left in a trailing column is a note on the week -- a race name
        # written beside the run that recorded it, a vacation.
        for name, col in result.trailing_days:
            text = cell(col)
            if col in used_columns or not text or NON_RUN.match(text):
                continue
            annotations.append(f"{name}: {text}")

        for run in week_runs:
            run["distance_mi"] = parse_workout_distance(run["text"])

        # A guided run like "Nike 1,2,3 Go" states no distance. If it's the only
        # unknown in a completed week, the weekly total gives it away exactly.
        counted = [r for r in week_runs if r["status"] == "completed"]
        unknown = [r for r in counted if r["distance_mi"] is None]
        if len(unknown) == 1 and sheet_miles is not None:
            known = sum(r["distance_mi"] or 0 for r in counted if r is not unknown[0])
            derived = round(sheet_miles - known, 2)
            if derived > 0:
                unknown[0]["distance_mi"] = derived
                result.derived_distances.append(
                    f"week {week_number}: {unknown[0]['text']} = {derived} mi"
                )

        if sheet_miles is not None and counted and not unknown:
            mine = round(sum(r["distance_mi"] or 0 for r in counted), 2)
            if abs(mine - sheet_miles) > 0.1:  # ignore the runner's own rounding
                result.discrepancies.append(
                    f"week {week_number}: day cells sum to {mine} mi, "
                    f"sheet says {sheet_miles} mi"
                )

        for run in week_runs:
            distance = run["distance_mi"]
            duration = run["duration_sec"]
            pace = (duration / distance) if (distance and duration) else None
            result.rows.append({
                "run_key": f"{SOURCE}:{run['date']}:{run['day'].lower()}",
                "source": SOURCE,
                "date": run["date"],
                "status": run["status"],
                "week_number": week_number,
                "distance_mi": distance,
                "duration_sec": duration,
                "pace_sec_per_mi": pace,
                "workout_type": classify(run["text"], distance=distance),
                "notes": run["text"],
                "imported_at": imported_at,
            })
            result.__dict__[
                {"completed": "completed", "skipped": "skipped", "planned": "planned"}[run["status"]]
            ] += 1

        result.weeks.append({
            "week_key": week_key,
            "source": SOURCE,
            "week_number": week_number,
            "week_start": week_start.isoformat(),
            "sheet_miles": sheet_miles,
            "sheet_seconds": sheet_seconds,
            "weight_lb": weight,
            "notes": "; ".join(annotations) or None,
            "imported_at": imported_at,
        })

    return result
