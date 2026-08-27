"""Value parsers.

Training spreadsheets are written by humans, so every field arrives in three or
four shapes. Each parser here takes whatever the cell held and returns one
canonical type, or None if the cell was empty/unparseable. Nothing raises on bad
input -- a garbled cell should cost you one field, not the whole row.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from dateutil import parser as dateparser

KM_PER_MILE = 1.609344
FEET_PER_METER = 3.280839895

_BLANK = {"", "-", "--", "n/a", "na", "none", "null", "?", "tbd", "rest"}


def is_blank(value) -> bool:
    return value is None or str(value).strip().lower() in _BLANK


def _clean(value) -> str:
    return str(value).strip().replace("–", "-").replace("’", "'")


def parse_date(value, *, default_year: int | None = None) -> date | None:
    """Handles 2026-08-24, 8/24/2026, Aug 24, 'Sun 8/24', and Excel serials."""
    if is_blank(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = _clean(value)

    # Excel/Sheets serial date (days since 1899-12-30).
    if re.fullmatch(r"\d{5}(\.\d+)?", text):
        from datetime import timedelta

        return (datetime(1899, 12, 30) + timedelta(days=float(text))).date()

    # Strip a leading weekday name: "Sun 8/24" or "Sunday, Aug 24".
    text = re.sub(
        r"^(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*[\s,]+",
        "",
        text,
        flags=re.I,
    )
    default = datetime(default_year or datetime.now().year, 1, 1)
    try:
        return dateparser.parse(text, default=default).date()
    except (ValueError, OverflowError, TypeError):
        return None


def parse_duration(value) -> int | None:
    """Seconds. Accepts 1:23:45, 45:30, '1h 5m', '45 min', or a bare number of minutes."""
    if is_blank(value):
        return None
    text = _clean(value).lower()

    # Colon forms: h:mm:ss, mm:ss.
    if ":" in text:
        parts = [p.strip() for p in text.split(":")]
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        if len(nums) == 3:
            h, m, s = nums
        elif len(nums) == 2:
            h, m, s = 0, nums[0], nums[1]
        else:
            return None
        return int(round(h * 3600 + m * 60 + s))

    # Unit forms: "1h 5m 30s", "45 min", "90s".
    if re.search(r"[hms]", text):
        total = 0.0
        found = False
        for amount, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(hr?|hours?|m|min(?:ute)?s?|s|sec(?:ond)?s?)", text):
            found = True
            n = float(amount)
            if unit.startswith("h"):
                total += n * 3600
            elif unit.startswith("s"):
                total += n
            else:
                total += n * 60
        if found:
            return int(round(total))

    # Bare number: minutes, the convention in hand-kept run logs.
    try:
        return int(round(float(text) * 60))
    except ValueError:
        return None


def parse_distance(value, *, unit_hint: str = "mi") -> float | None:
    """Miles. Accepts 6.2, '6.2 mi', '10 km', '10k', '5000 m'."""
    if is_blank(value):
        return None
    text = _clean(value).lower()

    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    number = float(match.group(1))
    rest = text[match.end():].strip()

    if rest.startswith("km") or rest.startswith("k"):
        return number / KM_PER_MILE
    if rest.startswith("mi"):
        return number
    if rest.startswith("m"):
        # "5000 m" is metres; "6.2 m" in a run log means miles.
        return number / 1000 / KM_PER_MILE if number > 100 else number
    # No unit in the cell -- fall back to the unit implied by the column header.
    return number / KM_PER_MILE if unit_hint == "km" else number


def parse_pace(value, *, unit_hint: str = "mi") -> float | None:
    """Seconds per mile. Accepts '8:42', '8:42/mi', '5:23 /km'."""
    if is_blank(value):
        return None
    text = _clean(value).lower()
    per_km = "km" in text or unit_hint == "km"

    match = re.search(r"(\d+):(\d{1,2})", text)
    if match:
        seconds = int(match.group(1)) * 60 + int(match.group(2))
    else:
        try:
            seconds = float(text) * 60  # decimal minutes
        except ValueError:
            return None
    return seconds * KM_PER_MILE if per_km else float(seconds)


def parse_number(value) -> float | None:
    if is_blank(value):
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", _clean(value).replace(",", ""))
    return float(match.group(0)) if match else None


def parse_int(value) -> int | None:
    number = parse_number(value)
    return int(round(number)) if number is not None else None


def parse_elevation(value, *, unit_hint: str = "ft") -> float | None:
    """Feet."""
    if is_blank(value):
        return None
    text = _clean(value).lower()
    number = parse_number(text)
    if number is None:
        return None
    if "m" in text and "mi" not in text:
        return number * FEET_PER_METER
    return number * FEET_PER_METER if unit_hint == "m" else number


# --- formatting helpers, used by the templates -------------------------------

def format_duration(seconds) -> str:
    if seconds is None:
        return "-"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_pace(seconds_per_mile) -> str:
    if not seconds_per_mile:
        return "-"
    m, s = divmod(int(round(seconds_per_mile)), 60)
    return f"{m}:{s:02d}"


# --- Apple Health quantities -------------------------------------------------
#
# Every quantity in a Health export carries its own unit attribute, and which
# one you get depends on the phone's locale -- the same field is `mi` on one
# device and `km` on another. Nothing here assumes; the unit always comes from
# the data.

_TO_MILES = {
    "mi": 1.0,
    "km": 1 / KM_PER_MILE,
    "m": 1 / 1000 / KM_PER_MILE,
    "yd": 1 / 1760,
    "ft": 1 / 5280,
}

_TO_FEET = {
    "ft": 1.0,
    "m": FEET_PER_METER,
    "cm": FEET_PER_METER / 100,
    "km": FEET_PER_METER * 1000,
    "mi": 5280.0,
    "in": 1 / 12,
}

_TO_KCAL = {"kcal": 1.0, "Cal": 1.0, "cal": 0.001, "kJ": 0.239006}


def parse_hk_quantity(value, unit: str | None, *, to: str) -> float | None:
    """Convert one HealthKit quantity into the unit this app stores.

    `to` is one of "mi", "ft", "degF", "kcal", or "raw" (a bare number such as
    bpm or a step count).
    """
    number = parse_number(value)
    if number is None:
        return None
    unit = (unit or "").strip()

    if to == "mi":
        return number * _TO_MILES.get(unit, 1.0)
    if to == "ft":
        return number * _TO_FEET.get(unit, 1.0)
    if to == "kcal":
        return number * _TO_KCAL.get(unit, 1.0)
    if to == "degF":
        # Temperature needs an offset, so it can't ride the factor tables.
        if unit.lower() in ("degc", "c", "°c"):
            return number * 9 / 5 + 32
        return number
    return number


def parse_hk_datetime(text) -> datetime | None:
    """'2026-08-22 07:14:03 -0500' -> an aware datetime.

    The wall-clock half is already the device's local time; the offset is there
    to tell you which local time. Do NOT convert to UTC before taking the date,
    or an early-morning run slides to the previous day and a late-evening one
    into the next training week.
    """
    if is_blank(text):
        return None
    value = _clean(text)
    for pattern in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            continue
    try:  # GPX track points use ISO-8601 with a Z
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def local_date(moment: datetime | None) -> date | None:
    """The calendar date as the runner experienced it. See parse_hk_datetime."""
    return moment.date() if moment else None
