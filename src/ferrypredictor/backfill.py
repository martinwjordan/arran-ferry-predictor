"""backfill job: archived Open-Meteo forecasts as they were issued 1 to 5 days ahead, for training.

Source: Open-Meteo's Previous Runs API. `<variable>_previous_dayN` is the value forecast N days
(24N hours) before the valid time, so training can use forecasts exactly as they stood at each
lead day, with no look-ahead. Archive starts found on 23 November 2026: GFS January 2024; ECMWF
0.25 degree February 2024 (no gusts); UK Met Office global and 2 km August 2024 (2 km: lead days 1-2
only); ECMWF 9 km January 2025.
Months before a model's archive starts come back empty and are recorded as done with no rows.
Lead day 0 (the current run) is not archived, so same-day forecasts come from the live log only.

Top-up (the `archive` job, after each forecast run): the training models' archive is extended from
the day after END to two days ago, so retraining and live labels always have matching forecasts.
Progress per model is kept in the same state file under "_through".

Work is split into (model, month) chunks. Each chunk is written as its own part file and then
recorded in data/state/backfill_done.json, so a stopped run resumes where it left off. Runs stop
cleanly, without failing, when Open-Meteo's hourly or daily limit is reached.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa

from . import config
from .forecast import ForecastError, _Http
from .health import update_health
from .storage import append_rows, load_state, save_state

ARCHIVE_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
MODELS = ["gfs_seamless", "ecmwf_ifs025", "ecmwf_ifs", "ukmo_global_deterministic_10km", "ukmo_uk_deterministic_2km"]
LEAD_DAYS = range(1, 6)
START = date(2024, 1, 1)
END = date(2026, 11, 23)  # the live forecast log covers later dates
VARS = {"wind_speed_10m": "wind_speed_kn", "wind_gusts_10m": "wind_gust_kn", "wind_direction_10m": "wind_dir_deg"}
TS = pa.timestamp("us", tz="UTC")

ARCHIVE_SCHEMA = pa.schema(
    [
        pa.field("model", pa.string(), nullable=False),
        pa.field("point", pa.string(), nullable=False),
        pa.field("valid_time", TS, nullable=False),
        pa.field("lead_day", pa.int8(), nullable=False),
        pa.field("wind_speed_kn", pa.float64()),
        pa.field("wind_gust_kn", pa.float64()),
        pa.field("wind_dir_deg", pa.float64()),
        pa.field("grid_lat", pa.float64()),
        pa.field("grid_lon", pa.float64()),
        pa.field("fetched_at", TS, nullable=False),
    ]
)


class RateLimited(Exception):
    """Open-Meteo's hourly or daily limit: stop for now, resume on the next run."""


def months(start: date, end: date) -> list[tuple[date, date]]:
    """Calendar-month chunks from start to end inclusive, clipped at both ends."""
    out, first = [], start
    while first <= end:
        next_month = (first.replace(day=1) + timedelta(days=32)).replace(day=1)
        out.append((first, min(end, next_month - timedelta(days=1))))
        first = next_month
    return out


def chunk_key(model: str, first: date) -> str:
    return f"{model}|{first:%Y-%m}"


def hourly_variables() -> str:
    return ",".join(f"{v}_previous_day{n}" for v in VARS for n in LEAD_DAYS)


def rows_from(model: str, payload, names: list[str], fetched_at: datetime) -> list[dict]:
    if isinstance(payload, dict):
        payload = [payload]
    if len(payload) != len(names):
        raise ForecastError(f"{model}: expected {len(names)} locations, got {len(payload)}")
    out = []
    for name, loc in zip(names, payload):
        hourly = loc.get("hourly") or {}
        times = hourly.get("time", [])
        for n in LEAD_DAYS:
            columns = {col: hourly.get(f"{var}_previous_day{n}") or [None] * len(times) for var, col in VARS.items()}
            for i, t in enumerate(times):
                values = {col: series[i] for col, series in columns.items()}
                if all(v is None for v in values.values()):
                    continue  # outside this model's archive or horizon
                out.append(
                    {
                        "model": model,
                        "point": name,
                        "valid_time": datetime.fromisoformat(t).replace(tzinfo=timezone.utc),
                        "lead_day": n,
                        **values,
                        "grid_lat": loc.get("latitude"),
                        "grid_lon": loc.get("longitude"),
                        "fetched_at": fetched_at,
                    }
                )
    return out


def _fetch(http: _Http, params: dict, minute_wait: float) -> dict | list:
    for attempt in (1, 2):
        try:
            return http.get_json(ARCHIVE_URL, params, attempts=2)
        except ForecastError as e:
            message = str(e)
            if "HTTP 429" not in message:
                raise
            if "inutely" in message and attempt == 1:
                time.sleep(minute_wait)  # per-minute limit: wait it out once
                continue
            raise RateLimited(message) from e
    raise RateLimited("per-minute limit persisted")


TOP_UP_MODELS = ["gfs_seamless", "ecmwf_ifs025"]  # the two the model trains on
TOP_UP_LAG = timedelta(days=2)


def _base_params(points: dict) -> tuple[list[str], dict]:
    names = list(points)
    return names, {
        "latitude": ",".join(str(points[n].lat) for n in names),
        "longitude": ",".join(str(points[n].lon) for n in names),
        "hourly": hourly_variables(),
        "wind_speed_unit": "kn",
        "cell_selection": "sea",
        "timezone": "GMT",
    }


def run_top_up(
    contact_email: str,
    now: datetime,
    data_dir: Path,
    site_dir: Path,
    models: list[str] | None = None,
    pause: float = 4.0,
    retry_wait: float = 10.0,
    minute_wait: float = 65.0,
) -> dict:
    """Extend the archive for the training models up to two days ago. A no-op once it is up to date."""
    data_dir, site_dir = Path(data_dir), Path(site_dir)
    names, base = _base_params(config.load_points())
    http = _Http(contact_email, retry_wait)
    state_path = data_dir / "state" / "backfill_done.json"
    done = load_state(state_path, {})
    through = done.setdefault("_through", {})
    target = now.date() - TOP_UP_LAG
    fetched, rows_written, stopped = 0, 0, None
    for model in models or TOP_UP_MODELS:
        start = date.fromisoformat(through.get(model, END.isoformat())) + timedelta(days=1)
        for first, last in months(start, target):
            if fetched and pause:
                time.sleep(pause)
            params = {**base, "models": model, "start_date": first.isoformat(), "end_date": last.isoformat()}
            try:
                rows = rows_from(model, _fetch(http, params, minute_wait), names, now)
            except RateLimited as e:
                stopped = str(e)
                break
            except ForecastError as e:
                message = f"top-up {model} {first}..{last}: {e}"
                update_health(site_dir, "archive", now, ok=False, details={"error": message})
                raise ForecastError(message) from e
            append_rows(data_dir / "forecast_archive", rows, ARCHIVE_SCHEMA, now)
            through[model] = last.isoformat()
            save_state(state_path, done)  # after the data is on disk
            fetched += 1
            rows_written += len(rows)
        if stopped:
            break
    summary = {"requests": fetched, "rows": rows_written, "through": dict(through), "target": target.isoformat()}
    if stopped:
        summary["stopped"] = stopped
    update_health(site_dir, "archive", now, details=summary)
    return summary


def run_backfill(
    contact_email: str,
    now: datetime,
    data_dir: Path,
    site_dir: Path,
    max_chunks: int = 40,
    models: list[str] | None = None,
    start: date = START,
    end: date = END,
    pause: float = 4.0,
    retry_wait: float = 10.0,
    minute_wait: float = 65.0,
) -> dict:
    data_dir, site_dir = Path(data_dir), Path(site_dir)
    names, base = _base_params(config.load_points())
    http = _Http(contact_email, retry_wait)
    state_path = data_dir / "state" / "backfill_done.json"
    done = load_state(state_path, {})
    chunks = [(m, first, last) for m in (models or MODELS) for first, last in months(start, end)]
    todo = [c for c in chunks if chunk_key(c[0], c[1]) not in done]

    fetched, rows_written, stopped = 0, 0, None
    for i, (model, first, last) in enumerate(todo[:max_chunks]):
        if i and pause:
            time.sleep(pause)
        params = {**base, "models": model, "start_date": first.isoformat(), "end_date": last.isoformat()}
        try:
            payload = _fetch(http, params, minute_wait)
            rows = rows_from(model, payload, names, now)
        except RateLimited as e:
            stopped = str(e)
            break
        except ForecastError as e:
            save_state(state_path, done)
            message = f"{chunk_key(model, first)}: {e}"
            update_health(site_dir, "backfill", now, ok=False, details={"error": message})
            raise ForecastError(message) from e
        append_rows(data_dir / "forecast_archive", rows, ARCHIVE_SCHEMA, now)
        done[chunk_key(model, first)] = {"rows": len(rows), "fetched_at": now.isoformat(timespec="seconds")}
        save_state(state_path, done)  # after the data is on disk, so a crash never skips a chunk
        fetched += 1
        rows_written += len(rows)

    summary = {
        "chunks_fetched": fetched,
        "rows": rows_written,
        "chunks_done": sum(1 for c in chunks if chunk_key(c[0], c[1]) in done),
        "chunks_total": len(chunks),
    }
    if stopped:
        summary["stopped"] = stopped
    update_health(site_dir, "backfill", now, details=summary)
    return summary
