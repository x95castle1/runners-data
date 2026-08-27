"""Runners Data -- a local browser for your training log."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ingest.normalize import format_duration, format_pace

from . import edits, stats
from .db import get_conn, has_data

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent


def load_dotenv(path: Path = PROJECT_DIR / ".env") -> None:
    """Minimal .env support so `make dev` sees the same settings as the container.

    Real environment variables always win -- docker-compose sets them directly.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()

app = FastAPI(title="Runners Data", docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

templates = Jinja2Templates(directory=BASE_DIR / "templates")


def static_url(path: str) -> str:
    """/static/<path> stamped with the file's mtime.

    Without this a browser keeps serving the CSS and JS it cached earlier, and a
    change lands as a half-updated page -- a stylesheet missing the rule that
    gives the map its height renders a collapsed, invisible map. The stamp
    changes whenever the file does, so the browser always fetches the new one.
    """
    relative = path.lstrip("/")
    try:
        stamp = int((BASE_DIR / "static" / relative).stat().st_mtime)
    except OSError:
        stamp = 0
    return f"/static/{relative}?v={stamp}"


templates.env.globals["static_url"] = static_url


@app.middleware("http")
async def revalidate_pages(request: Request, call_next):
    """Let the browser cache assets hard, but always recheck the page itself.

    Assets are cache-busted by static_url(), so they are safe to keep; the HTML
    that references them must never be stale or it points at old versions.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response

RACE_NAME = os.environ.get("RACE_NAME", "Race day")
RACE_DATE = os.environ.get("RACE_DATE", "")


def fmt_date(value, pattern: str = "%a %b %-d") -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(str(value)).strftime(pattern)
    except ValueError:
        return str(value)


def fmt_signed_pace(seconds) -> str:
    """A gap from average, as +m:ss or -m:ss."""
    if seconds is None:
        return ""
    sign = "+" if seconds >= 0 else "-"
    whole = int(round(abs(seconds)))
    return f"{sign}{whole // 60}:{whole % 60:02d}"


def fmt_number(value, digits: int = 1) -> str:
    return f"{value:,.{digits}f}" if value is not None else "-"


SOURCE_LABELS = {
    "sheet-plan": "your training plan",
    "sheet-csv": "your spreadsheet",
    "apple-health": "Apple Health",
}


def source_label(value: str) -> str:
    return SOURCE_LABELS.get(value, value)


templates.env.filters.update(
    source=source_label,
    duration=format_duration,
    pace=format_pace,
    day=fmt_date,
    num=fmt_number,
    signed=fmt_signed_pace,
)


SORTABLE = ["date", "workout_type", "distance_mi", "duration_sec",
            "pace_sec_per_mi", "avg_hr", "elevation_ft"]


def sort_links(filters: dict) -> dict:
    """Header links that keep the active filters and flip direction on re-click."""
    scoped = {k: v for k, v in filters.items()
              if k in ("start", "end", "type", "status") and v}
    links = {}
    for field in SORTABLE:
        active = filters["sort"] == field
        direction = ("asc" if filters["dir"] == "desc" else "desc") if active else "desc"
        links[field] = {
            "url": "/runs?" + urlencode({**scoped, "sort": field, "dir": direction}),
            "arrow": (" \u2193" if filters["dir"] == "desc" else " \u2191") if active else "",
        }
    return links


def status_links(filters: dict, counts: dict) -> list[dict]:
    scoped = {k: v for k, v in filters.items()
              if k in ("start", "end", "type") and v}
    options = [("completed", "Completed"), ("planned", "Planned"),
               ("skipped", "Skipped"), ("all", "All")]
    links = []
    for value, label in options:
        n = sum(counts.values()) if value == "all" else counts.get(value, 0)
        if not n and value != "completed":
            continue
        links.append({
            "label": f"{label} ({n})",
            "url": "/runs?" + urlencode({**scoped, "status": value}),
            "active": filters["status"] == value,
        })
    return links


def quick_ranges(filters: dict) -> list[dict]:
    today = date.today()
    scoped = {k: v for k, v in filters.items()
              if k in ("type", "status") and v and not (k == "status" and v == "completed")}
    ranges = []
    for label, days in (("Last 7 days", 7), ("Last 30 days", 30), ("Last 90 days", 90)):
        start = (today - timedelta(days=days - 1)).isoformat()
        ranges.append({
            "label": label,
            "url": "/runs?" + urlencode({**scoped, "start": start, "end": today.isoformat()}),
            "active": filters["start"] == start and filters["end"] == today.isoformat(),
        })
    ranges.append({
        "label": "All time",
        "url": "/runs?" + urlencode(scoped) if scoped else "/runs",
        "active": not filters["start"] and not filters["end"],
    })
    return ranges


def race_countdown() -> dict | None:
    if not RACE_DATE:
        return None
    try:
        target = datetime.fromisoformat(RACE_DATE).date()
    except ValueError:
        return None
    days = (target - date.today()).days
    return {
        "name": RACE_NAME,
        "date": target.isoformat(),
        "days": days,
        "weeks": round(days / 7, 1),
        "past": days < 0,
    }


OPTIONAL_COLUMNS = ["avg_hr", "elevation_ft"]

# Below this many samples there is no shape to plot, only noise. Apple keeps
# dense per-second heart rate for recent workouts and thins older ones down to
# a handful of background readings plus the summary, so most older runs land
# here -- they get the summary treatment instead of an empty chart.
MIN_HR_SAMPLES = 20


def present_columns(rows: list[dict]) -> set[str]:
    """Which optional columns any row actually fills in.

    A plan-style sheet records no heart rate or elevation; showing a column of
    dashes just costs the table width.
    """
    return {c for c in OPTIONAL_COLUMNS if any(r.get(c) is not None for r in rows)}


def base_context(request: Request, conn) -> dict:
    return {
        "request": request,
        "has_data": has_data(conn),
        "race": race_countdown(),
        "today": date.today().isoformat(),
        "type_slots": stats.type_slots(conn),
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, scope: str = "block"):
    conn = get_conn()
    try:
        # Apple Health brings in running that predates this marathon block, so
        # the dashboard scopes to the block unless asked for everything.
        block_start = stats.training_block_start(conn)
        since = block_start if (scope != "all" and block_start) else None
        context = base_context(request, conn)
        context.update(
            scope=scope,
            block_start=block_start,
            has_older=stats.has_runs_before(conn, block_start) if block_start else False,
            summary=stats.summary(conn, since),
            this_week=stats.week_summary(conn),
            last_week=stats.week_summary(conn, offset_weeks=1),
            weekly=stats.weekly(conn, weeks=16, since=since),
            load=stats.daily_load(conn, window=7, days=180, since=since),
            paces=stats.pace_series(conn, since=since),
            trend=stats.pace_trend(stats.pace_series(conn, since=since)),
            bests=stats.personal_bests(conn, since),
            recent=stats.list_runs(conn, limit=8),
            upcoming=stats.upcoming(conn, limit=5),
            present=present_columns(stats.list_runs(conn, limit=8)),
            counts=stats.status_counts(conn),
        )
        return templates.TemplateResponse("dashboard.html", context)
    finally:
        conn.close()


@app.get("/runs", response_class=HTMLResponse)
def runs_page(
    request: Request,
    start: str | None = None,
    end: str | None = None,
    type: str | None = None,
    status: str = "completed",
    sort: str = "date",
    dir: str = "desc",
):
    conn = get_conn()
    try:
        rows = stats.list_runs(conn, start=start, end=end, workout_type=type,
                               status=status, sort=sort, direction=dir)
        context = base_context(request, conn)
        context.update(
            runs=rows,
            totals=stats.summary(conn, start, end, type, status),
            types=stats.workout_types(conn),
            counts=stats.status_counts(conn),
            present=present_columns(rows),
        )
        context["filters"] = {"start": start or "", "end": end or "",
                              "type": type or "", "status": status,
                              "sort": sort, "dir": dir}
        context["sortcols"] = sort_links(context["filters"])
        context["quick_ranges"] = quick_ranges(context["filters"])
        context["status_links"] = status_links(context["filters"], context["counts"])
        return templates.TemplateResponse("runs.html", context)
    finally:
        conn.close()


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int, logged: str | None = None,
               error: str | None = None):
    conn = get_conn()
    try:
        run = stats.get_run(conn, run_id)
        if not run:
            context = base_context(request, conn)
            context["run_id"] = run_id
            return templates.TemplateResponse("not_found.html", context, status_code=404)
        prev_id, next_id = stats.neighbours(conn, run)
        points = stats.route(conn, run_id)
        context = base_context(request, conn)
        samples = stats.hr_series(conn, run_id)
        run_splits = stats.splits(conn, run_id)
        weathered = [x for x in run_splits if x.get("apparent_f") is not None]
        context.update(
            hr=samples if len(samples) >= MIN_HR_SAMPLES else [],
            hr_sparse=0 < len(samples) < MIN_HR_SAMPLES,
            splits=run_splits,
            split_scale=stats.deviation_scale(run_splits),
            elev_scale=stats.elevation_scale(run_splits),
            split_hr=any(x.get("avg_hr") for x in run_splits),
            average_pace=(run["duration_sec"] / run["distance_mi"]
                          if run.get("duration_sec") and run.get("distance_mi") else None),
            split_weather=bool(weathered),
            weather_range=({
                "low": min(x["apparent_f"] for x in weathered),
                "high": max(x["apparent_f"] for x in weathered),
                "humidity": weathered[0]["humidity_pct"],
            } if weathered else None),
            track=stats.track_for_map(points),
        )
        extra = json.loads(run["extra"]) if run.get("extra") else None
        context.update(run=run, prev_id=prev_id, next_id=next_id, extra=extra,
                       logged=logged, error=error)
        return templates.TemplateResponse("run_detail.html", context)
    finally:
        conn.close()


@app.post("/runs/{run_id}/log")
def log_run(run_id: int,
            action: str = Form("log"),
            duration: str = Form(""),
            distance: str = Form(""),
            notes: str = Form("")):
    """Record a time against a run from the run detail page."""
    conn = get_conn()
    try:
        if action == "skip":
            edits.mark_skipped(conn, run_id)
            return RedirectResponse(f"/runs/{run_id}?logged=skipped", status_code=303)
        if action == "unlog":
            edits.unlog(conn, run_id)
            return RedirectResponse(f"/runs/{run_id}?logged=cleared", status_code=303)
        try:
            edits.log_run(conn, run_id, duration_text=duration,
                          distance_text=distance, notes=notes or None)
        except edits.InvalidEntry as problem:
            return RedirectResponse(
                f"/runs/{run_id}?error={quote(str(problem))}", status_code=303)
        return RedirectResponse(f"/runs/{run_id}?logged=1", status_code=303)
    finally:
        conn.close()


# --- JSON API ---------------------------------------------------------------

@app.get("/api/runs")
def api_runs(start: str | None = None, end: str | None = None,
             type: str | None = None, status: str = "completed",
             limit: int = Query(1000, le=10000)):
    conn = get_conn()
    try:
        return stats.list_runs(conn, start=start, end=end, workout_type=type,
                               status=status, limit=limit)
    finally:
        conn.close()


@app.get("/api/runs/{run_id}/series")
def api_series(run_id: int):
    """Heart rate, splits and the GPS track for one run."""
    conn = get_conn()
    try:
        return {
            "heart_rate": stats.hr_series(conn, run_id),
            "splits": stats.splits(conn, run_id),
            "route": stats.route(conn, run_id),
        }
    finally:
        conn.close()


@app.get("/api/weeks")
def api_weeks():
    """The spreadsheet's own weekly rows: its totals, and body weight."""
    conn = get_conn()
    try:
        return stats.plan_weeks(conn)
    finally:
        conn.close()


@app.get("/api/summary")
def api_summary(start: str | None = None, end: str | None = None):
    conn = get_conn()
    try:
        return {
            "summary": stats.summary(conn, start, end),
            "this_week": stats.week_summary(conn),
            "bests": stats.personal_bests(conn),
            "race": race_countdown(),
        }
    finally:
        conn.close()


@app.get("/api/weekly")
def api_weekly(weeks: int = 52):
    conn = get_conn()
    try:
        return stats.weekly(conn, weeks=weeks)
    finally:
        conn.close()


@app.get("/api/load")
def api_load(window: int = 7, days: int = 365):
    conn = get_conn()
    try:
        return stats.daily_load(conn, window=window, days=days)
    finally:
        conn.close()


@app.get("/healthz")
def healthz():
    conn = get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        return JSONResponse({"status": "ok", "runs": count})
    finally:
        conn.close()
