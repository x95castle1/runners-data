"""Builds a small Apple Health export.zip standing in for the real thing.

Covers what the real export throws at the loader: both XML generations, metric
and imperial units, one run recorded twice by two apps, a workout with a GPS
route and one without, a two-a-day, and heart-rate Records that fall inside and
outside the workout windows.
"""

from __future__ import annotations

import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

CENTRAL = timezone(timedelta(hours=-5))
ROOT = "apple_health_export"


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S %z")


def gpx(start: datetime, points: int = 30, climb_ft: float = 60.0) -> str:
    body = []
    lat, lon, ele = 41.8781, -87.6298, 180.0
    for i in range(points):
        lat += 0.0004
        lon += 0.0003
        ele += (climb_ft / 3.28084) / points
        moment = start.astimezone(timezone.utc) + timedelta(seconds=i * 10)
        body.append(
            f'<trkpt lat="{lat:.6f}" lon="{lon:.6f}"><ele>{ele:.1f}</ele>'
            f'<time>{moment.strftime("%Y-%m-%dT%H:%M:%SZ")}</time>'
            f'<extensions><speed>2.9</speed></extensions></trkpt>')
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1">'
            f'<trk><name>Route</name><trkseg>{"".join(body)}</trkseg></trk></gpx>')


def modern_workout(uuid, source, start, minutes, miles, *, hr=(120, 150, 172),
                   route=None, temp="72.5 degF", humidity="6300 %",
                   elevation="4572 cm", steps=None) -> str:
    end = start + timedelta(minutes=minutes)
    parts = [
        f'<Workout workoutActivityType="HKWorkoutActivityTypeRunning"',
        f' HKWorkoutUUID="{uuid}" sourceName="{source}" duration="{minutes}"',
        f' durationUnit="min" startDate="{stamp(start)}" endDate="{stamp(end)}">',
        f'<WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning"'
        f' sum="{miles}" unit="mi"/>',
        f'<WorkoutStatistics type="HKQuantityTypeIdentifierActiveEnergyBurned"'
        f' sum="{int(miles * 105)}" unit="kcal"/>',
    ]
    if hr:
        low, avg, high = hr
        parts.append(
            f'<WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate"'
            f' average="{avg}" minimum="{low}" maximum="{high}" unit="count/min"/>')
    if steps:
        parts.append(f'<WorkoutStatistics type="HKQuantityTypeIdentifierStepCount"'
                     f' sum="{steps}" unit="count"/>')
    if temp:
        parts.append(f'<MetadataEntry key="HKWeatherTemperature" value="{temp}"/>')
    if humidity:
        parts.append(f'<MetadataEntry key="HKWeatherHumidity" value="{humidity}"/>')
    if elevation:
        parts.append(f'<MetadataEntry key="HKElevationAscended" value="{elevation}"/>')
    if route:
        parts.append(f'<WorkoutRoute sourceName="{source}">'
                     f'<FileReference path="/workout-routes/{route}"/></WorkoutRoute>')
    parts.append('</Workout>')
    return "".join(parts)


def legacy_workout(uuid, source, start, minutes, km) -> str:
    """Pre-iOS 15: distance and energy live on the element, in metric."""
    end = start + timedelta(minutes=minutes)
    return (
        f'<Workout workoutActivityType="HKWorkoutActivityTypeRunning"'
        f' HKWorkoutUUID="{uuid}" sourceName="{source}" duration="{minutes}"'
        f' durationUnit="min" totalDistance="{km}" totalDistanceUnit="km"'
        f' totalEnergyBurned="410" totalEnergyBurnedUnit="kcal"'
        f' startDate="{stamp(start)}" endDate="{stamp(end)}"/>')


def hr_records(start: datetime, minutes: int, bpm: int, every_sec: int = 30) -> str:
    out = []
    for offset in range(0, minutes * 60, every_sec):
        moment = start + timedelta(seconds=offset)
        wobble = bpm + (offset // every_sec % 7) - 3
        out.append(
            f'<Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Apple Watch"'
            f' unit="count/min" value="{wobble}" startDate="{stamp(moment)}"'
            f' endDate="{stamp(moment)}"/>')
    return "".join(out)


def build(target: Path) -> Path:
    d = lambda y, m, day, hh, mm: datetime(y, m, day, hh, mm, tzinfo=CENTRAL)

    long_run = d(2026, 8, 22, 6, 30)        # matches the plan's Saturday 18 Miles
    easy_run = d(2026, 8, 24, 6, 15)        # matches Monday 4 Miles Easy
    double_am = d(2026, 8, 20, 6, 0)        # two-a-day, morning
    double_pm = d(2026, 8, 20, 18, 30)      # two-a-day, evening
    pre_plan = d(2025, 11, 2, 8, 0)         # older than week 1, legacy XML, metric
    late_night = d(2026, 8, 18, 23, 40)     # would roll to the next day in UTC

    workouts = [
        modern_workout("UUID-LONG", "Apple Watch", long_run, 235, 18.02,
                       hr=(118, 149, 171), route="route_2026-08-22_6.30am.gpx",
                       steps=33000),
        # The same long run, also recorded by Nike Run Club: no route, no HR.
        modern_workout("UUID-LONG-NRC", "Nike Run Club",
                       long_run + timedelta(minutes=1), 233, 17.95, hr=None,
                       temp=None, humidity=None, elevation=None),
        modern_workout("UUID-EASY", "Apple Watch", easy_run, 51, 4.01,
                       hr=(112, 138, 152), temp="64.4 degF", humidity="8100 %"),
        modern_workout("UUID-AM", "Apple Watch", double_am, 32, 3.0),
        modern_workout("UUID-PM", "Apple Watch", double_pm, 44, 4.5),
        modern_workout("UUID-LATE", "Apple Watch", late_night, 25, 2.5),
        legacy_workout("UUID-OLD", "Apple Watch", pre_plan, 47, 8.05),
    ]

    records = (hr_records(long_run, 235, 149)
               + hr_records(easy_run, 51, 138)
               # A resting sample far from any workout: must not be attached.
               + hr_records(d(2026, 8, 23, 14, 0), 2, 62))

    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<!DOCTYPE HealthData [<!ELEMENT HealthData (Record|Workout)*>]>'
           '<HealthData locale="en_US">'
           '<ExportDate value="2026-08-26 09:00:00 -0500"/>'
           + records + "".join(workouts) + '</HealthData>')

    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{ROOT}/{XML}", xml)
        archive.writestr(f"{ROOT}/export_cda.xml", "<ClinicalDocument/>")
        archive.writestr(f"{ROOT}/workout-routes/route_2026-08-22_6.30am.gpx",
                         gpx(long_run, points=40, climb_ft=210))
    return target


XML = "export.xml"

if __name__ == "__main__":
    import sys
    print(build(Path(sys.argv[1] if len(sys.argv) > 1 else "health-export.zip")))
