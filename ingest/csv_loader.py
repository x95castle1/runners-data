"""CSV adapter: a spreadsheet export -> canonical run records.

Handles the things real exports do -- a title row above the headers, blank
spacer rows, rest days with no distance, merged-cell debris in trailing columns.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import normalize as nz
from .schema import FIELDS, match_columns, unit_hint

SOURCE = "sheet-csv"

# Parser for each canonical field. unit_hint is passed where it matters.
_PARSERS = {
    "date":            lambda v, u: nz.parse_date(v),
    "distance_mi":     lambda v, u: nz.parse_distance(v, unit_hint=u),
    "duration_sec":    lambda v, u: nz.parse_duration(v),
    "pace_sec_per_mi": lambda v, u: nz.parse_pace(v, unit_hint=u),
    "avg_hr":          lambda v, u: nz.parse_int(v),
    "max_hr":          lambda v, u: nz.parse_int(v),
    "elevation_ft":    lambda v, u: nz.parse_elevation(v, unit_hint=u),
    "cadence":         lambda v, u: nz.parse_int(v),
    "calories":        lambda v, u: nz.parse_int(v),
}


@dataclass
class LoadResult:
    rows: list[dict] = field(default_factory=list)
    mapping: dict[str, str] = field(default_factory=dict)
    unmapped: list[str] = field(default_factory=list)
    header_row: int = 0
    rows_read: int = 0
    skipped_no_date: int = 0
    skipped_no_activity: int = 0
    warnings: list[str] = field(default_factory=list)


def _read_grid(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [row for row in csv.reader(fh)]


def find_header_row(grid: list[list[str]], scan: int = 12) -> tuple[int, dict, list]:
    """The header row is the first one whose cells map to a date plus >=2 fields.

    Sheets commonly open with a title or a blank row, so we can't assume row 0.
    """
    best = (0, {}, [])
    best_score = -1
    for i, row in enumerate(grid[:scan]):
        headers = [c.strip() for c in row]
        if not any(headers):
            continue
        mapping, unmapped = match_columns(headers)
        score = len(mapping) + (5 if "date" in mapping else 0)
        if score > best_score:
            best_score, best = score, (i, mapping, unmapped)
    return best


def _derive(record: dict) -> None:
    """Fill in whichever of distance/duration/pace the sheet left out."""
    dist, dur, pace = (record.get("distance_mi"), record.get("duration_sec"),
                       record.get("pace_sec_per_mi"))
    if dist and dur and not pace:
        record["pace_sec_per_mi"] = dur / dist
    elif dist and pace and not dur:
        record["duration_sec"] = int(round(pace * dist))
    elif dur and pace and not dist:
        record["distance_mi"] = dur / pace

    # A pace column that disagrees with distance/duration by more than 5% is
    # usually a stale formula; trust the two measured values over the derived one.
    dist, dur, pace = (record.get("distance_mi"), record.get("duration_sec"),
                       record.get("pace_sec_per_mi"))
    if dist and dur and pace:
        computed = dur / dist
        if abs(computed - pace) / computed > 0.05:
            record["pace_sec_per_mi"] = computed


def load_csv(path: str | Path, *, overrides: dict[str, str] | None = None) -> LoadResult:
    path = Path(path)
    grid = _read_grid(path)
    result = LoadResult()
    if not grid:
        result.warnings.append(f"{path.name} is empty")
        return result

    header_row, mapping, unmapped = find_header_row(grid)

    for canonical, header in (overrides or {}).items():
        if header == "":
            mapping.pop(canonical, None)
        else:
            mapping[canonical] = header
    if overrides:
        unmapped = [h for h in unmapped if h not in overrides.values()]

    result.header_row, result.mapping, result.unmapped = header_row, mapping, unmapped

    if "date" not in mapping:
        result.warnings.append(
            f"{path.name}: no date column found. Add one to config/columns.json, "
            'e.g. {"date": "Your Header"}'
        )
        return result

    headers = [c.strip() for c in grid[header_row]]
    index = {h: i for i, h in enumerate(headers)}
    hints = {f: unit_hint(h, f) for f, h in mapping.items()}
    imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    seq_by_date: dict[str, int] = {}

    for raw in grid[header_row + 1:]:
        if not any(c.strip() for c in raw):
            continue
        result.rows_read += 1

        def cell(header: str) -> str:
            i = index.get(header, -1)
            return raw[i].strip() if 0 <= i < len(raw) else ""

        record: dict = {}
        for canonical in FIELDS:
            header = mapping.get(canonical)
            if not header:
                continue
            value = cell(header)
            parser = _PARSERS.get(canonical)
            record[canonical] = (
                parser(value, hints.get(canonical, "mi")) if parser
                else (value or None)
            )

        run_date = record.get("date")
        if run_date is None:
            result.skipped_no_date += 1
            continue
        _derive(record)

        if not record.get("distance_mi") and not record.get("duration_sec"):
            result.skipped_no_activity += 1
            continue

        extra = {h: cell(h) for h in unmapped if cell(h)}
        iso = run_date.isoformat()
        seq_by_date[iso] = seq_by_date.get(iso, 0) + 1

        record["date"] = iso
        record["source"] = SOURCE
        record["run_key"] = f"{SOURCE}:{iso}:{seq_by_date[iso]}"
        record["extra"] = json.dumps(extra) if extra else None
        record["imported_at"] = imported_at
        result.rows.append(record)

    return result
