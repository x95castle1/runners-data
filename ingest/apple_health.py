"""Adapter for an Apple Health export.

`Health app -> profile picture -> Export All Health Data` produces an
`export.zip` laid out like:

    apple_health_export/
        export.xml              every sample the phone has ever recorded
        export_cda.xml          the same data as a clinical document -- ignored
        workout-routes/*.gpx    one track per workout that had GPS

Only `<Workout workoutActivityType="HKWorkoutActivityTypeRunning">` matters here.
export.xml routinely runs past 100 MB and several hundred thousand `Record`
elements, so it is streamed with iterparse and cleared as it goes rather than
loaded into a tree.

Like the other loaders this one only reads: it returns parsed workouts and lets
`ingest.cli` decide what to write.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from . import normalize as nz

SOURCE = "apple-health"
RUNNING = "HKWorkoutActivityTypeRunning"

XML_NAME = "export.xml"
ROUTES_DIR = "workout-routes"

STAT_DISTANCE = "HKQuantityTypeIdentifierDistanceWalkingRunning"
STAT_HEART_RATE = "HKQuantityTypeIdentifierHeartRate"
STAT_ENERGY = "HKQuantityTypeIdentifierActiveEnergyBurned"
STAT_STEPS = "HKQuantityTypeIdentifierStepCount"
RECORD_HEART_RATE = STAT_HEART_RATE

M_PER_S_TO_MPH = 2.2369362920544


@dataclass
class HealthResult:
    workouts: list[dict] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    seen: int = 0                    # running workouts before de-duplication
    routes_found: int = 0
    hr_series_found: int = 0


# --- small parsing helpers ---------------------------------------------------

def tag_of(element) -> str:
    """Strip the XML namespace GPX files carry and export.xml doesn't."""
    return element.tag.rsplit("}", 1)[-1]


def split_quantity(text) -> tuple[str, str]:
    """'150 cm' -> ('150', 'cm'). Metadata values pack both into one string."""
    match = re.match(r"^\s*(-?[\d.]+)\s*(.*)$", str(text or ""))
    return (match.group(1), match.group(2).strip()) if match else (str(text or ""), "")


def metadata_quantity(value, *, to: str) -> float | None:
    number, unit = split_quantity(value)
    return nz.parse_hk_quantity(number, unit, to=to)


# --- export.xml --------------------------------------------------------------

def _stream(source):
    """iterparse that actually stays flat in memory.

    `element.clear()` empties an element but leaves it attached to the root, so
    the root's child list grows for the whole file -- on a multi-gigabyte export
    that is the difference between 60 MB of memory and running out of it. Clearing
    the root after each top-level element is what keeps it flat.
    """
    context = ET.iterparse(source, events=("start", "end"))
    _, root = next(context)
    depth = 0
    for event, element in context:
        if event == "start":
            depth += 1
            continue
        depth -= 1
        # Only top-level elements: a Workout's own children close first, and
        # clearing then would wipe its statistics before they are read.
        if depth == 0:
            yield element
            element.clear()
            root.clear()


def _read_workouts(stream) -> tuple[list[dict], list[str]]:
    """Pass one: every running workout, with its statistics and route reference."""
    workouts, warnings = [], []
    for element in _stream(stream):
        if tag_of(element) != "Workout":
            continue
        if element.get("workoutActivityType") != RUNNING:
            continue

        started = nz.parse_hk_datetime(element.get("startDate"))
        ended = nz.parse_hk_datetime(element.get("endDate"))
        if started is None:
            warnings.append(f"workout with an unreadable startDate: {element.get('startDate')!r}")
            continue

        workout = {
            "health_id": element.get("HKWorkoutUUID") or element.get("UUID"),
            "source_name": element.get("sourceName"),
            "started": started,
            "ended": ended,
            "duration_sec": None,
            "distance_mi": None,
            "calories": None,
            "avg_hr": None, "max_hr": None, "min_hr": None,
            "elevation_ft": None, "temperature_f": None, "humidity_pct": None,
            "cadence": None,
            "route_file": None,
            "hr_samples": [], "route": [],
        }

        duration = nz.parse_number(element.get("duration"))
        if duration is not None:
            unit = (element.get("durationUnit") or "min").lower()
            workout["duration_sec"] = int(round(duration * (60 if unit.startswith("min") else 1)))
        elif ended is not None:
            workout["duration_sec"] = int(round((ended - started).total_seconds()))

        # Older exports (pre-iOS 15) put distance and energy on the Workout
        # element itself; newer ones use WorkoutStatistics. Both still turn up.
        if element.get("totalDistance"):
            workout["distance_mi"] = nz.parse_hk_quantity(
                element.get("totalDistance"), element.get("totalDistanceUnit"), to="mi")
        if element.get("totalEnergyBurned"):
            workout["calories"] = nz.parse_hk_quantity(
                element.get("totalEnergyBurned"), element.get("totalEnergyBurnedUnit"), to="kcal")

        steps = None
        for child in element:
            name = tag_of(child)
            if name == "WorkoutStatistics":
                kind, unit = child.get("type"), child.get("unit")
                if kind == STAT_DISTANCE and child.get("sum"):
                    workout["distance_mi"] = nz.parse_hk_quantity(child.get("sum"), unit, to="mi")
                elif kind == STAT_HEART_RATE:
                    workout["avg_hr"] = nz.parse_int(child.get("average"))
                    workout["max_hr"] = nz.parse_int(child.get("maximum"))
                    workout["min_hr"] = nz.parse_int(child.get("minimum"))
                elif kind == STAT_ENERGY and child.get("sum"):
                    workout["calories"] = nz.parse_hk_quantity(child.get("sum"), unit, to="kcal")
                elif kind == STAT_STEPS and child.get("sum"):
                    steps = nz.parse_number(child.get("sum"))
            elif name == "MetadataEntry":
                key, value = child.get("key"), child.get("value")
                if key == "HKWeatherTemperature":
                    workout["temperature_f"] = metadata_quantity(value, to="degF")
                elif key == "HKWeatherHumidity":
                    humidity = metadata_quantity(value, to="raw")
                    # HealthKit stores humidity as percent x 100 -- "6300 %" is 63%.
                    if humidity is not None:
                        workout["humidity_pct"] = humidity / 100 if humidity > 100 else humidity
                elif key == "HKElevationAscended":
                    workout["elevation_ft"] = metadata_quantity(value, to="ft")
            elif name == "WorkoutRoute":
                for reference in child:
                    if tag_of(reference) == "FileReference" and reference.get("path"):
                        workout["route_file"] = reference.get("path").lstrip("/")

        if steps and workout["duration_sec"]:
            workout["cadence"] = int(round(steps / (workout["duration_sec"] / 60)))

        if not workout["health_id"]:
            # Fall back to the start time; unique in practice for one person.
            workout["health_id"] = f"start:{started.isoformat()}"

        workouts.append(workout)
    return workouts, warnings


def _attach_hr_series(stream, workouts: list[dict]) -> int:
    """Pass two: bucket heart-rate Records into the workout windows.

    One sweep of the file for all workouts -- re-scanning per workout would mean
    reading 100 MB once per run.
    """
    windows = sorted(
        ((w["started"], w["ended"] or w["started"], w) for w in workouts),
        key=lambda item: item[0],
    )
    if not windows:
        return 0

    starts = [w[0] for w in windows]
    import bisect

    earliest = windows[0][0]
    latest = max(w[1] for w in windows)

    attached = 0
    for element in _stream(stream):
        if tag_of(element) != "Record":
            continue
        if element.get("type") != RECORD_HEART_RATE:
            continue
        moment = nz.parse_hk_datetime(element.get("startDate"))
        bpm = nz.parse_int(element.get("value"))
        if moment is None or bpm is None or not (earliest <= moment <= latest):
            continue

        # The last window beginning at or before this sample, and the one before
        # it -- back-to-back runs can leave a sample inside the earlier window.
        index = bisect.bisect_right(starts, moment) - 1
        for candidate in (index, index - 1):
            if candidate < 0:
                continue
            begin, finish, workout = windows[candidate]
            if begin <= moment <= finish:
                workout["hr_samples"].append(
                    (int((moment - begin).total_seconds()), bpm))
                attached += 1
                break

    for workout in workouts:
        workout["hr_samples"].sort()
    return attached


# --- GPX ---------------------------------------------------------------------

def parse_gpx(data: bytes, started) -> list[tuple]:
    """(offset_sec, lat, lon, altitude_ft, speed_mph) for each track point."""
    points = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return points

    for element in root.iter():
        if tag_of(element) != "trkpt":
            continue
        lat = nz.parse_number(element.get("lat"))
        lon = nz.parse_number(element.get("lon"))
        if lat is None or lon is None:
            continue
        altitude_ft = speed_mph = None
        moment = None
        for child in element.iter():
            name = tag_of(child)
            if name == "ele":
                metres = nz.parse_number(child.text)
                altitude_ft = metres * nz.FEET_PER_METER if metres is not None else None
            elif name == "time":
                moment = nz.parse_hk_datetime(child.text)
            elif name == "speed":
                metres_per_sec = nz.parse_number(child.text)
                speed_mph = (metres_per_sec * M_PER_S_TO_MPH
                             if metres_per_sec is not None else None)
        offset = 0
        if moment is not None and started is not None:
            # GPX times are UTC; the workout start is local-with-offset. Both are
            # aware, so subtracting is correct without normalizing either.
            offset = int((moment - started).total_seconds())
        points.append((offset, lat, lon, altitude_ft, speed_mph))
    return points


# --- de-duplication ----------------------------------------------------------

def overlap_ratio(a: dict, b: dict) -> float:
    a_end = a["ended"] or a["started"]
    b_end = b["ended"] or b["started"]
    latest_start = max(a["started"], b["started"])
    earliest_end = min(a_end, b_end)
    shared = (earliest_end - latest_start).total_seconds()
    if shared <= 0:
        return 0.0
    shortest = min((a_end - a["started"]).total_seconds(),
                   (b_end - b["started"]).total_seconds())
    return shared / shortest if shortest > 0 else 0.0


def richness(workout: dict) -> tuple:
    """Which of two recordings of the same run to keep.

    Judged on what pass one already knows, because de-duplication has to happen
    before heart-rate samples are attached -- overlapping windows would otherwise
    hand a run's samples to the copy that is about to be thrown away.
    """
    return (
        1 if workout.get("route_file") else 0,
        1 if workout.get("avg_hr") else 0,
        1 if workout.get("distance_mi") else 0,
        workout.get("duration_sec") or 0,
    )


def deduplicate(workouts: list[dict], threshold: float = 0.7) -> tuple[list[dict], list[str]]:
    """Collapse the same run recorded by two apps (Nike Run Club and the Watch).

    Every collapse is reported -- a run silently vanishing is worse than a
    duplicate you can see.
    """
    ordered = sorted(workouts, key=lambda w: w["started"])
    kept: list[dict] = []
    notes: list[str] = []
    for workout in ordered:
        match = next((k for k in kept if overlap_ratio(k, workout) >= threshold), None)
        if match is None:
            kept.append(workout)
            continue
        winner, loser = ((workout, match) if richness(workout) > richness(match)
                         else (match, workout))
        if winner is not match:
            kept[kept.index(match)] = winner
        notes.append(
            f"{winner['started'].date()}: two recordings "
            f"({match['source_name']} / {workout['source_name']}) — "
            f"kept {winner['source_name']}"
        )
    return kept, notes


# --- entry point -------------------------------------------------------------

def load_export(path: str | Path, *, with_routes: bool = True,
                with_hr: bool = True) -> HealthResult:
    path = Path(path)
    result = HealthResult()

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            xml_name = next((n for n in names if n.endswith(XML_NAME)
                             and "cda" not in n.lower()), None)
            if xml_name is None:
                result.warnings.append(f"{path.name}: no {XML_NAME} inside the zip")
                return result
            with archive.open(xml_name) as stream:
                workouts, warnings = _read_workouts(stream)
            result.warnings.extend(warnings)
            result.seen = len(workouts)
            kept, result.duplicates = deduplicate(workouts)
            if kept and with_hr:
                with archive.open(xml_name) as stream:
                    result.hr_series_found = _attach_hr_series(stream, kept)
            if with_routes:
                _load_routes_from_zip(archive, names, kept, result)
    else:
        with path.open("rb") as stream:
            workouts, warnings = _read_workouts(stream)
        result.warnings.extend(warnings)
        result.seen = len(workouts)
        kept, result.duplicates = deduplicate(workouts)
        if kept and with_hr:
            with path.open("rb") as stream:
                result.hr_series_found = _attach_hr_series(stream, kept)
        if with_routes:
            _load_routes_from_dir(path.parent, kept, result)

    result.workouts = [_finalize(w) for w in kept]
    return result


def _load_routes_from_zip(archive, names, workouts, result) -> None:
    by_suffix = {n.rsplit("/", 1)[-1]: n for n in names if n.endswith(".gpx")}
    for workout in workouts:
        name = _route_name(workout, by_suffix)
        if name is None:
            continue
        workout["route"] = parse_gpx(archive.read(name), workout["started"])
        if workout["route"]:
            result.routes_found += 1


def _load_routes_from_dir(directory: Path, workouts, result) -> None:
    routes = directory / ROUTES_DIR
    if not routes.is_dir():
        return
    by_suffix = {p.name: p for p in routes.glob("*.gpx")}
    for workout in workouts:
        name = _route_name(workout, by_suffix)
        if name is None:
            continue
        workout["route"] = parse_gpx(Path(name).read_bytes(), workout["started"])
        if workout["route"]:
            result.routes_found += 1


def _route_name(workout: dict, by_suffix: dict):
    """Routes are linked by FileReference path, not derivable from the timestamp."""
    reference = workout.get("route_file")
    if not reference:
        return None
    return by_suffix.get(reference.rsplit("/", 1)[-1])


def _finalize(workout: dict) -> dict:
    started = workout["started"]
    distance, duration = workout["distance_mi"], workout["duration_sec"]
    workout["date"] = nz.local_date(started).isoformat()
    workout["started_at"] = started.isoformat()
    workout["pace_sec_per_mi"] = (duration / distance) if (distance and duration) else None
    workout["route_points"] = len(workout["route"])
    workout["source"] = SOURCE
    workout["measured_source"] = SOURCE
    # Unit conversions leave long float tails; nobody needs 149.99999999940002 ft.
    for field_name, places in (("distance_mi", 3), ("elevation_ft", 1),
                               ("temperature_f", 1), ("humidity_pct", 0),
                               ("pace_sec_per_mi", 2)):
        if workout.get(field_name) is not None:
            workout[field_name] = round(workout[field_name], places)

    if workout["elevation_ft"] is None and workout["route"]:
        # No HKElevationAscended metadata -- sum the climbs out of the track.
        gain = 0.0
        altitudes = [p[3] for p in workout["route"] if p[3] is not None]
        for previous, current in zip(altitudes, altitudes[1:]):
            if current > previous:
                gain += current - previous
        workout["elevation_ft"] = round(gain, 1) if altitudes else None
    return workout
