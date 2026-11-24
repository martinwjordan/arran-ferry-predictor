"""site job: build the data file behind the phone page.

For every sailing in the next five days (UK dates): the scheduled times, CalMac's current word on
it (from the parsed notices of active notice versions, and whether CalMac has removed it from its
timetable), the forecast wind and waves mid-crossing, and the chance it runs from the predict job
(site/predictions.json), when that is fresh.

Wind comes from the UK Met Office 2 km model where its latest run covers the crossing, otherwise
ECMWF. Waves come from Open-Meteo's marine model. All times on the page are UK time.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import parse
from .collect import NOTICE_SCHEMA, ROUTE_STATUS_SCHEMA, SAILING_SCHEMA
from .config import REPO_ROOT, load_routes
from .forecast import FORECAST_SCHEMA
from .status import ORDER
from .storage import read_dataset

UK = parse.UK
DAYS = 5
MID_POINT = {"003": "mid_ardrossan", "301": "mid_troon"}
WIND_MODELS = ["ukmo_uk_deterministic_2km", "ecmwf_ifs"]  # first that covers the crossing wins
EVENT_ORDER = ["cancelled", "diverted", "at_risk_diversion", "at_risk", "reinstated"]
WEB_DIR = REPO_ROOT / "web"
PREDICTIONS_MAX_AGE = timedelta(hours=3)  # older than this and the page shows no chances rather than stale ones


def load_predictions(site_dir: Path, now: datetime) -> tuple[dict[str, dict], dict | None]:
    """Chances by sailing key, and what they were made from. Empty if missing or stale."""
    try:
        p = json.loads((Path(site_dir) / "predictions.json").read_text())
        made = datetime.fromisoformat(p["generated_at"])
    except (OSError, ValueError, KeyError):
        return {}, None
    if now - made > PREDICTIONS_MAX_AGE:
        return {}, None
    chances = {
        s["key"]: {"p": s["p_runs"], "show": s["show"], "band": s["band"], "lead_day": s["lead_day"],
                   "p_weather": s["p_weather"], "p_other": s["p_non_weather"]}
        for s in p.get("sailings", []) if s.get("p_runs") is not None
    }
    meta = {"generated_at": p["generated_at"], "model_as_of": p.get("model_as_of"),
            "data_through": p.get("data_through"), "runs": p.get("runs", {})}
    return chances, meta


def _hour(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def latest_forecasts(rows: list[dict]) -> dict[str, dict[tuple, dict]]:
    """Per model, the latest run, indexed by (point, valid hour). Marine rows have no run time."""
    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_model[r["model"]].append(r)
    out = {}
    for model, rs in by_model.items():
        stamp = (lambda r: r["fetched_at"]) if model == "marine" else (lambda r: (r["run_time"] or r["fetched_at"]))
        newest = max(stamp(r) for r in rs)
        out[model] = {(r["point"], r["valid_time"]): r for r in rs if stamp(r) == newest}
    return out


def crossing_hours(depart: datetime, arrive: datetime | None) -> list[datetime]:
    arrive = arrive or depart + timedelta(hours=1)
    hours, t = [], _hour(depart)
    while t <= arrive:
        hours.append(t)
        t += timedelta(hours=1)
    return hours


def wind_for(route: str, depart: datetime, arrive: datetime | None, fc: dict) -> dict | None:
    point, hours = MID_POINT.get(route), crossing_hours(depart, arrive)
    for model in WIND_MODELS:
        grid = fc.get(model, {})
        rows = [grid[(point, h)] for h in hours if (point, h) in grid]
        if not rows:
            # ECMWF goes 3-hourly further out: accept the nearest value within 2 hours
            near = [r for (p, t), r in grid.items() if p == point and abs((t - hours[0]).total_seconds()) <= 7200]
            rows = sorted(near, key=lambda r: abs((r["valid_time"] - hours[0]).total_seconds()))[:1]
        rows = [r for r in rows if r["wind_speed_kn"] is not None]
        if rows:
            gusts = [r["wind_gust_kn"] for r in rows if r["wind_gust_kn"] is not None]
            return {
                "model": model,
                "issued": (rows[0]["run_time"] or rows[0]["fetched_at"]).isoformat(),
                "speed_kn": round(max(r["wind_speed_kn"] for r in rows)),
                "gust_kn": round(max(gusts)) if gusts else None,
                "dir_deg": rows[0]["wind_dir_deg"],
            }
    return None


def waves_for(route: str, depart: datetime, arrive: datetime | None, fc: dict) -> dict | None:
    point, grid = MID_POINT.get(route), fc.get("marine", {})
    rows = [grid[(point, h)] for h in crossing_hours(depart, arrive) if (point, h) in grid]
    rows = [r for r in rows if r["wave_height_m"] is not None]
    if not rows:
        return None
    top = max(rows, key=lambda r: r["wave_height_m"])
    return {"height_m": round(top["wave_height_m"], 1), "period_s": top["wave_period_s"]}


def active_versions(notices: list[dict], now: datetime) -> dict[str, dict]:
    """Latest version of each SAILING notice that is still live."""
    latest: dict[str, dict] = {}
    for n in notices:
        if n.get("notice_type") != "SAILING":
            continue
        cur = latest.get(n["notice_id"])
        if cur is None or (n["updated_at"] or n["first_seen_at"]) > (cur["updated_at"] or cur["first_seen_at"]):
            latest[n["notice_id"]] = n
    return {i: n for i, n in latest.items() if n.get("is_active") and (n.get("end_at") is None or n["end_at"] > now)}


def current_events(events: list[dict], live: dict[str, dict]) -> dict[str, set[str]]:
    if not events:
        return {}
    version = max(e["parser_version"] for e in events)
    out: dict[str, set[str]] = defaultdict(set)
    for e in events:
        n = live.get(e["notice_id"])
        if e["parser_version"] == version and n and e["notice_updated_at"] == n["updated_at"] and e["sailing_key"]:
            out[e["sailing_key"]].add(e["event_type"])
    return out


def _plain(markdown: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", markdown or "")  # links: keep the words
    text = text.replace("**", "").replace("__", "").replace("\r", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def route_summaries(status_rows: list[dict], live: dict[str, dict]) -> list[dict]:
    latest: dict[str, dict] = {}
    for r in status_rows:
        if r["route_code"] not in latest or r["observed_at"] > latest[r["route_code"]]["observed_at"]:
            latest[r["route_code"]] = r
    out = []
    for route in load_routes():
        notices = sorted((n for n in live.values() if n["route_code"] == route.code),
                         key=lambda n: ORDER.index(n["sub_status"]) if n["sub_status"] in ORDER else 1, reverse=True)
        row = latest.get(route.code, {})
        out.append(
            {
                "code": route.code,
                "name": " to ".join(route.ports),
                "status": row.get("effective_status") or row.get("status"),
                "notices": [
                    {"title": (n["title"] or "").strip(), "level": n["sub_status"], "reason": n.get("reason"),
                     "updated": n["updated_at"].isoformat() if n["updated_at"] else None, "text": _plain(n["detail"])}
                    for n in notices
                ],
            }
        )
    return out


def build(now: datetime, data_dir: Path, site_dir: Path) -> dict:
    data_dir, site_dir = Path(data_dir), Path(site_dir)
    timetable = parse.build_timetable(read_dataset(data_dir / "sailings", SAILING_SCHEMA).to_pylist())
    sailing_rows = {}
    for r in read_dataset(data_dir / "sailings", SAILING_SCHEMA).to_pylist():
        if r.get("departure_at"):
            sailing_rows[r.get("sailing_key") or parse.sailing_key(r["route_code"], r["origin"], r["departure_at"])] = r
    notices = read_dataset(data_dir / "notices", NOTICE_SCHEMA).to_pylist()
    live = active_versions(notices, now)
    events = current_events(read_dataset(data_dir / "events", parse.EVENT_SCHEMA).to_pylist(), live)
    fc = latest_forecasts(read_dataset(data_dir / "forecasts", FORECAST_SCHEMA).to_pylist())
    chances, model = load_predictions(site_dir, now)

    today = now.astimezone(UK).date()
    days = []
    for offset in range(DAYS):
        day = today + timedelta(days=offset)
        sailings = []
        for s in (s for s in timetable if s.date == day):
            row = sailing_rows.get(s.key, {})
            depart, arrive = row.get("departure_at"), row.get("arrival_at")
            kinds = sorted(events.get(s.key, set()), key=EVENT_ORDER.index)
            sailings.append(
                {
                    "key": s.key,
                    "route": s.route,
                    "from": s.origin,
                    "to": s.destination,
                    "depart": s.local,
                    "arrive": arrive.astimezone(UK).strftime("%H:%M") if arrive else None,
                    "departed": bool(depart and depart < now),
                    "removed": s.removed,
                    "calmac": kinds,
                    "wind": wind_for(s.route, depart, arrive, fc) if depart else None,
                    "waves": waves_for(s.route, depart, arrive, fc) if depart else None,
                    "chance": None if depart and depart < now else chances.get(s.key),
                }
            )
        sailings.sort(key=lambda x: (x["route"], x["depart"]))
        days.append({"date": day.isoformat(), "label": f"{day:%A} {day.day} {day:%B}", "sailings": sailings})

    health = {}
    for f in sorted((site_dir / "health").glob("*.json")):
        try:
            health[f.stem] = json.loads(f.read_text())
        except ValueError:
            pass
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "routes": route_summaries(read_dataset(data_dir / "route_status", ROUTE_STATUS_SCHEMA).to_pylist(), live),
        "days": days,
        "model": model,
        "health": {k: {"last_success": v.get("last_success"), "error": v.get("error")} for k, v in health.items()},
    }


def run_site(now: datetime, data_dir: Path, site_dir: Path, out_dir: Path) -> dict:
    """Write the page and its data into out_dir, ready to publish."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = build(now, data_dir, site_dir)
    if WEB_DIR.exists():
        for f in WEB_DIR.iterdir():
            if f.is_file():
                shutil.copy2(f, out_dir / f.name)
    tmp = out_dir / "sailings.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str))
    tmp.replace(out_dir / "sailings.json")
    return {"days": len(payload["days"]), "sailings": sum(len(d["sailings"]) for d in payload["days"])}
