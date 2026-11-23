"""collect job: log CalMac notices, sailings and route status, writing only what changed."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa

from . import config
from .calmac import Route, Sailing
from .health import update_health
from .status import effective_status, normalise_reason
from .storage import append_rows, load_state, save_state

UK = ZoneInfo("Europe/London")
TS = pa.timestamp("us", tz="UTC")
HORIZON_DAYS = 7

NOTICE_SCHEMA = pa.schema(
    [
        pa.field("notice_id", pa.string(), nullable=False),
        pa.field("route_code", pa.string(), nullable=False),
        pa.field("updated_at", TS, nullable=False),
        pa.field("first_seen_at", TS, nullable=False),
        pa.field("notice_type", pa.string(), nullable=False),
        pa.field("sub_status", pa.string()),
        pa.field("reason", pa.string()),
        pa.field("reason_raw", pa.string()),
        pa.field("title", pa.string()),
        pa.field("detail", pa.string()),
        pa.field("start_at", TS),
        pa.field("end_at", TS),
        pa.field("next_review_at", TS),
        pa.field("is_active", pa.bool_()),
    ]
)

SAILING_SCHEMA = pa.schema(
    [
        pa.field("sailing_id", pa.int64(), nullable=False),
        pa.field("route_code", pa.string(), nullable=False),
        pa.field("change_type", pa.string(), nullable=False),
        pa.field("observed_at", TS, nullable=False),
        pa.field("sailing_date", pa.date32()),
        pa.field("origin", pa.string()),
        pa.field("destination", pa.string()),
        pa.field("departure_at", TS),
        pa.field("arrival_at", TS),
        pa.field("rescheduled_departure_at", TS),
        pa.field("rescheduled_arrival_at", TS),
        pa.field("status", pa.string()),
    ]
)

ROUTE_STATUS_SCHEMA = pa.schema(
    [
        pa.field("route_code", pa.string(), nullable=False),
        pa.field("observed_at", TS, nullable=False),
        pa.field("status", pa.string()),
        pa.field("effective_status", pa.string()),
        pa.field("vessels", pa.string()),
    ]
)

# Fields compared to decide whether a sailing changed.
_SAILING_FIELDS = (
    "sailing_date",
    "origin",
    "destination",
    "departure_at",
    "arrival_at",
    "rescheduled_departure_at",
    "rescheduled_arrival_at",
    "status",
)


def _sailing_record(route_code: str, s: Sailing) -> dict:
    return {
        "sailing_id": s.id,
        "route_code": route_code,
        "sailing_date": s.sailing_date,
        "origin": s.origin_port.name,
        "destination": s.destination_port.name,
        "departure_at": s.departure_date_time,
        "arrival_at": s.arrival_date_time,
        "rescheduled_departure_at": s.rescheduled_departure_date_time,
        "rescheduled_arrival_at": s.rescheduled_arrival_date_time,
        "status": s.status,
    }


def _comparable(rec: dict) -> dict:
    return {k: (rec[k].isoformat() if hasattr(rec.get(k), "isoformat") else rec.get(k)) for k in _SAILING_FIELDS}


def _from_state(sailing_id: str, saved: dict) -> dict:
    out = {"sailing_id": int(sailing_id), "route_code": saved["route_code"]}
    for k in _SAILING_FIELDS:
        v = saved.get(k)
        if v and k == "sailing_date":
            v = datetime.fromisoformat(v).date()
        elif v and k.endswith("_at"):
            v = datetime.fromisoformat(v)
        out[k] = v
    return out


def _notice_rows(routes: list[Route], seen: set[str], now: datetime) -> tuple[list[dict], list[str]]:
    rows, keys = [], []
    for r in routes:
        for n in r.route_statuses:
            key = f"{n.id}|{n.updated_at_date_time.isoformat()}"
            if key in seen:
                continue
            keys.append(key)
            rows.append(
                {
                    "notice_id": n.id,
                    "route_code": r.route_code,
                    "updated_at": n.updated_at_date_time,
                    "first_seen_at": now,
                    "notice_type": n.status.upper(),
                    "sub_status": n.sub_status,
                    "reason": normalise_reason(n.disruption_reason),
                    "reason_raw": n.disruption_reason,
                    "title": n.title,
                    "detail": n.detail,
                    "start_at": n.start_date_time,
                    "end_at": n.end_date_time,
                    "next_review_at": n.next_review_date_time,
                    "is_active": n.is_active,
                }
            )
    return rows, keys


def _sailing_changes(
    current: dict[str, dict], last: dict[str, dict], now: datetime
) -> tuple[list[dict], dict[str, dict]]:
    rows = []
    for sid, rec in current.items():
        prev = last.get(sid)
        if prev is None:
            rows.append({**rec, "change_type": "new", "observed_at": now})
        elif _comparable(rec) != {k: prev.get(k) for k in _SAILING_FIELDS}:
            rows.append({**rec, "change_type": "changed", "observed_at": now})
    for sid, prev in last.items():
        if sid in current:
            continue
        departure = prev.get("departure_at")
        if departure and datetime.fromisoformat(departure) > now:
            rows.append({**_from_state(sid, prev), "change_type": "removed", "observed_at": now})
    new_state = {sid: {"route_code": rec["route_code"], **_comparable(rec)} for sid, rec in current.items()}
    return rows, new_state


def run_collect(client, now: datetime, data_dir: Path, site_dir: Path) -> dict:
    data_dir, site_dir = Path(data_dir), Path(site_dir)
    state_dir = data_dir / "state"
    codes = [r.code for r in config.load_routes()]

    routes = client.fetch_routes(codes)
    today = now.astimezone(UK).date()
    end = today + timedelta(days=HORIZON_DAYS + 1)
    current: dict[str, dict] = {}
    for code in codes:
        for s in client.fetch_sailings(code, today, end):
            current[str(s.id)] = _sailing_record(code, s)

    # Notices: one row per new version.
    seen_path = state_dir / "notices_seen.json"
    seen = set(load_state(seen_path, []))
    notice_rows, new_keys = _notice_rows(routes, seen, now)
    append_rows(data_dir / "notices", notice_rows, NOTICE_SCHEMA, now)

    # Sailings: new, changed, or removed before departure.
    last_path = state_dir / "sailings_last.json"
    last = load_state(last_path, {})
    sailing_rows, new_last = _sailing_changes(current, last, now)
    append_rows(data_dir / "sailings", sailing_rows, SAILING_SCHEMA, now)

    # Route status: only when status, effective status or vessel list changes.
    rs_path = state_dir / "route_status_last.json"
    rs_last = load_state(rs_path, {})
    rs_rows, rs_new = [], {}
    for r in routes:
        vessels = ", ".join(v.name for v in r.vessels)
        value = [r.status, effective_status(r, now), vessels]
        rs_new[r.route_code] = value
        if rs_last.get(r.route_code) != value:
            rs_rows.append(
                {"route_code": r.route_code, "observed_at": now, "status": value[0], "effective_status": value[1], "vessels": vessels}
            )
    append_rows(data_dir / "route_status", rs_rows, ROUTE_STATUS_SCHEMA, now)

    # State is saved only after every dataset write has succeeded.
    save_state(seen_path, sorted(seen | set(new_keys)))
    save_state(last_path, new_last)
    save_state(rs_path, rs_new)

    summary = {"notices": len(notice_rows), "sailings": len(sailing_rows), "route_status": len(rs_rows)}
    update_health(site_dir, "collect", now, details={**summary, "sailings_tracked": len(current)})
    return summary
