"""forecast job: log Open-Meteo wind and wave forecasts for the ports and mid-channel.

Open-Meteo data is licensed CC BY 4.0. ECMWF IFS is the backfillable model; the UK Met Office
2 km model adds detail for the first day or two. Each model is written once per model run.

Wind requests ask for sea grid cells. Open-Meteo's default picks a land cell, which put the
ECMWF mid-crossing points on one inland cell near Irvine (found 23 November 2026). Each row
records the grid cell it came from, and points sharing a cell are flagged in the job health.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pyarrow as pa

from . import __version__, config
from .calmac import REPO_URL
from .health import update_health
from .storage import append_rows, load_state, save_state

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
# ecmwf_ifs and the Met Office 2 km model add detail; gfs_seamless and ecmwf_ifs025 are the models the
# weather model is trained on (the only ones archived back to early 2024), so they must be logged live.
WIND_MODELS = ["ecmwf_ifs", "ukmo_uk_deterministic_2km", "gfs_seamless", "ecmwf_ifs025"]
# Open-Meteo publishes run metadata under the underlying model's name for some combined models.
META_MODEL = {"gfs_seamless": "ncep_gfs013"}
FORECAST_DAYS = 6
TS = pa.timestamp("us", tz="UTC")

FORECAST_SCHEMA = pa.schema(
    [
        pa.field("model", pa.string(), nullable=False),
        pa.field("run_time", TS),
        pa.field("fetched_at", TS, nullable=False),
        pa.field("point", pa.string(), nullable=False),
        pa.field("valid_time", TS, nullable=False),
        pa.field("lead_hours", pa.float64()),
        pa.field("wind_speed_kn", pa.float64()),
        pa.field("wind_gust_kn", pa.float64()),
        pa.field("wind_dir_deg", pa.float64()),
        pa.field("wave_height_m", pa.float64()),
        pa.field("wave_period_s", pa.float64()),
        pa.field("wave_dir_deg", pa.float64()),
        pa.field("grid_lat", pa.float64()),
        pa.field("grid_lon", pa.float64()),
    ]
)

_WIND_VARS = {"wind_speed_10m": "wind_speed_kn", "wind_gusts_10m": "wind_gust_kn", "wind_direction_10m": "wind_dir_deg"}
_WAVE_VARS = {"wave_height": "wave_height_m", "wave_period": "wave_period_s", "wave_direction": "wave_dir_deg"}


class ForecastError(RuntimeError):
    pass


def meta_url(model: str) -> str:
    return f"https://api.open-meteo.com/data/{META_MODEL.get(model, model)}/static/meta.json"


class _Http:
    def __init__(self, contact_email: str, retry_wait: float):
        self.retry_wait = retry_wait
        self.client = httpx.Client(
            timeout=30, headers={"User-Agent": f"arran-ferry-predictor/{__version__} (+{REPO_URL}; {contact_email})"}
        )

    def get_json(self, url: str, params: dict | None = None, attempts: int = 3):
        for attempt in range(1, attempts + 1):
            last = attempt == attempts
            try:
                r = self.client.get(url, params=params)
            except httpx.TransportError as e:
                if last:
                    raise ForecastError(f"network error: {e}") from e
                time.sleep(self.retry_wait * attempt)
                continue
            if (r.status_code == 429 or r.status_code >= 500) and not last:
                time.sleep(self.retry_wait * attempt)
                continue
            if r.status_code >= 400:
                reason = ""
                try:
                    reason = r.json().get("reason", "")
                except ValueError:
                    pass
                raise ForecastError(f"HTTP {r.status_code} {reason}".strip())
            return r.json()
        raise ForecastError("unreachable")


def _run_time(http: _Http, model: str) -> datetime | None:
    try:
        t = http.get_json(meta_url(model)).get("last_run_initialisation_time")
    except (ForecastError, ValueError, AttributeError):
        return None
    return datetime.fromtimestamp(t, timezone.utc) if isinstance(t, (int, float)) else None


def _rows(model, payload, names, variables, run_time, fetched_at) -> list[dict]:
    if isinstance(payload, dict):
        payload = [payload]
    if len(payload) != len(names):
        raise ForecastError(f"{model}: expected {len(names)} locations, got {len(payload)}")
    base = run_time or fetched_at
    first_hour = fetched_at.replace(minute=0, second=0, microsecond=0)
    out = []
    for name, loc in zip(names, payload):
        hourly = loc.get("hourly") or {}
        grid = {"grid_lat": loc.get("latitude"), "grid_lon": loc.get("longitude")}
        for i, t in enumerate(hourly.get("time", [])):
            valid = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
            if valid < first_hour:
                continue
            values = {col: (hourly.get(var) or [None] * (i + 1))[i] for var, col in variables.items()}
            if all(v is None for v in values.values()) and variables is _WIND_VARS:
                continue  # beyond this model's horizon
            row = {k: None for k in FORECAST_SCHEMA.names}
            row.update(
                model=model,
                run_time=run_time,
                fetched_at=fetched_at,
                point=name,
                valid_time=valid,
                lead_hours=(valid - base).total_seconds() / 3600,
                **values,
                **grid,
            )
            out.append(row)
    return out


def shared_cells(rows: list[dict]) -> dict[str, list[list[str]]]:
    """Per model, groups of points served by the same grid cell (they would get identical data)."""
    cells: dict[str, dict[tuple, set]] = defaultdict(lambda: defaultdict(set))
    for r in rows:
        if r["grid_lat"] is not None and r["grid_lon"] is not None:
            cells[r["model"]][(round(r["grid_lat"], 4), round(r["grid_lon"], 4))].add(r["point"])
    out = {}
    for model, by_cell in cells.items():
        groups = sorted(sorted(p) for p in by_cell.values() if len(p) > 1)
        if groups:
            out[model] = groups
    return out


def run_forecast(contact_email: str, now: datetime, data_dir: Path, site_dir: Path, retry_wait: float = 10.0) -> dict:
    data_dir, site_dir = Path(data_dir), Path(site_dir)
    points = config.load_points()
    names = list(points)
    coords = {
        "latitude": ",".join(str(points[n].lat) for n in names),
        "longitude": ",".join(str(points[n].lon) for n in names),
        "timezone": "GMT",
        "forecast_days": FORECAST_DAYS,
    }
    http = _Http(contact_email, retry_wait)
    state_path = data_dir / "state" / "forecast_last_run.json"
    last_runs = load_state(state_path, {})
    all_rows, counts, errors = [], {}, []

    for model in WIND_MODELS:
        counts[model] = 0
        run_time = _run_time(http, model)
        if run_time and last_runs.get(model) == run_time.isoformat():
            continue
        try:
            payload = http.get_json(
                FORECAST_URL,
                {**coords, "models": model, "hourly": ",".join(_WIND_VARS), "wind_speed_unit": "kn", "cell_selection": "sea"},
            )
            rows = _rows(model, payload, names, _WIND_VARS, run_time, now)
        except ForecastError as e:
            errors.append(f"{model}: {e}")
            continue
        all_rows += rows
        counts[model] = len(rows)
        if run_time:
            last_runs[model] = run_time.isoformat()

    counts["marine"] = 0
    try:
        payload = http.get_json(MARINE_URL, {**coords, "hourly": ",".join(_WAVE_VARS)})
        rows = _rows("marine", payload, names, _WAVE_VARS, None, now)
        all_rows += rows
        counts["marine"] = len(rows)
    except ForecastError as e:
        errors.append(f"marine: {e}")

    append_rows(data_dir / "forecasts", all_rows, FORECAST_SCHEMA, now)
    save_state(state_path, last_runs)
    summary = {"rows": counts, "run_times": last_runs}
    shared = shared_cells(all_rows)
    if shared:
        summary["shared_grid_cells"] = shared
    if errors:
        update_health(site_dir, "forecast", now, ok=False, details={"error": "; ".join(errors)})
        raise ForecastError("; ".join(errors))
    update_health(site_dir, "forecast", now, details=summary)
    return summary
