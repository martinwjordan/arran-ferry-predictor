"""labels job: what happened to each sailing, from CalMac's own notices and timetable, once its day has settled.

Rules (LABELLER_VERSION 1), for every sailing in the timetable we logged on a fully covered UK day:

1. Notice first. The latest definitive event any notice version gave the sailing wins: cancelled is
   cancelled_weather or cancelled_other by the notice's reason (weather or operational), diverted is
   diverted, reinstated is operated. "At risk" is never definitive.
2. Timetable next. No definitive event, but CalMac removed the sailing before it was due: cancelled,
   with the reason of the notice that flagged it at risk if any, otherwise cancelled_unknown. CalMac
   also drops sailings it replaces with a diversion timetable, so these are marked confidence
   "removal" and some are really diversions. A removal more than 72 hours ahead with no notice is a
   planned timetable change, not a disruption: kept, but not trained on or scored.
3. Otherwise operated: confidence "timetable", or "at_risk" when a notice had flagged it. On a day
   CalMac switched to a diversion timetable, some "at risk" sailings left in the timetable were in
   fact replaced; scoring reports confident labels (notice, timetable) separately for that reason.

Sailings CalMac added to the timetable less than 24 hours before departure (relief and diversion
sailings, such as Caledonian Isles running from Troon) are labelled but marked additional_unscheduled,
as in the FOI labels: not trained on or scored.

A day settles at the end of the following UK day, because notices often mention the previous
evening's sailings the next morning. Each settled day is written once to data/labels_live/ in the
same shape as the FOI labels (plus labeller_version, confidence and labelled_at), so training can
use both. Diverted sailings ran; scoring treats operated and diverted as "ran".
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pyarrow as pa

from .collect import ROUTE_STATUS_SCHEMA, SAILING_SCHEMA, sailing_key
from .foi import LABEL_SCHEMA
from .health import update_health
from .parse import EVENT_SCHEMA, UK, Sailing, build_timetable
from .predict import _route_overrides, operating_vessels
from .storage import append_rows, load_state, read_dataset, save_state

LABELLER_VERSION = 1
SOURCE = "live"
PLANNED_AHEAD = timedelta(hours=72)
DEFINITIVE = ("cancelled", "diverted", "reinstated")
TS = pa.timestamp("us", tz="UTC")

LIVE_LABEL_SCHEMA = pa.schema(
    list(LABEL_SCHEMA)
    + [
        pa.field("labeller_version", pa.int16(), nullable=False),
        pa.field("confidence", pa.string(), nullable=False),
        pa.field("labelled_at", TS, nullable=False),
    ]
)
ADDED_LATE = timedelta(hours=24)
RAN = ("operated", "diverted")
NOT_SCORED = ("planned_timetable_change", "additional_unscheduled")  # exclusions left out of scoring
CONFIDENT = ("notice", "timetable")


def departure_of(s: Sailing) -> datetime:
    return datetime.fromisoformat(s.key.split("|")[2])


def first_full_day(sailing_rows: list[dict]) -> date | None:
    """The first UK day whose whole timetable we saw before it began."""
    if not sailing_rows:
        return None
    first = min(r["observed_at"] for r in sailing_rows).astimezone(UK)
    midnight = datetime(first.year, first.month, first.day, tzinfo=UK)
    return first.date() if first == midnight else first.date() + timedelta(days=1)


def settled_through(now: datetime) -> date:
    """Days up to and including this one have settled (the whole of the following UK day has passed)."""
    return now.astimezone(UK).date() - timedelta(days=2)


def latest_definitive(events: list[dict]) -> dict[str, dict]:
    """Per sailing, the latest definitive notice event (cancelled beats diverted in the same notice version)."""
    rank = {"cancelled": 2, "diverted": 1, "reinstated": 0}
    out: dict[str, dict] = {}
    for e in events:
        if e["event_type"] not in DEFINITIVE or not e.get("sailing_key"):
            continue
        k = e["sailing_key"]
        key = (e["notice_updated_at"], rank[e["event_type"]])
        if k not in out or key > (out[k]["notice_updated_at"], rank[out[k]["event_type"]]):
            out[k] = e
    return out


def latest_reason(events: list[dict]) -> dict[str, str]:
    """Per sailing, the reason on the latest notice that mentioned it at all."""
    out: dict[str, tuple] = {}
    for e in events:
        k = e.get("sailing_key")
        if k and e.get("reason") and (k not in out or e["notice_updated_at"] > out[k][0]):
            out[k] = (e["notice_updated_at"], e["reason"])
    return {k: v[1] for k, v in out.items()}


def cancelled_label(reason: str | None) -> str:
    return {"weather": "cancelled_weather", "operational": "cancelled_other"}.get(reason or "", "cancelled_unknown")


def leg(origin: str, destination: str) -> str:
    return f"{origin}-{destination}"


def label_sailing(s: Sailing, definitive: dict | None, reason: str | None, flagged: bool) -> dict:
    """Label, operated leg, confidence, train flag and exclusion for one sailing."""
    departure = departure_of(s)
    operated_leg, exclusion = leg(s.origin, s.destination), None
    if definitive:
        kind, confidence = definitive["event_type"], "notice"
        if kind == "cancelled":
            label, operated_leg = cancelled_label(definitive.get("reason")), None
        elif kind == "diverted":
            label = "diverted"
            port = definitive.get("diversion_port")
            if port:
                operated_leg = leg(port, s.destination) if s.origin != "Brodick" else leg(s.origin, port)
        else:
            label = "operated"
    elif s.removed and s.removed_at is not None and s.removed_at < departure:
        operated_leg, confidence = None, "removal"
        if departure - s.removed_at > PLANNED_AHEAD and not flagged:
            label, exclusion = "planned_change", "planned_timetable_change"
        else:
            label = cancelled_label(reason)
    else:
        label, confidence = "operated", "at_risk" if flagged else "timetable"
    if label == "cancelled_unknown":
        exclusion = "cancellation_reason_unknown"
    return {"label": label, "operated_leg": operated_leg, "confidence": confidence,
            "train": exclusion is None, "exclusion": exclusion}


def vessel_at(status_rows: list[dict], route: str, when: datetime, overrides: dict[str, str]) -> str | None:
    rows = [r for r in status_rows if r["route_code"] == route and r["observed_at"] <= when]
    return operating_vessels(rows, overrides).get(route)


def first_seen(sailing_rows: list[dict]) -> tuple[dict[str, datetime], datetime | None]:
    """When each sailing first appeared in our log, and when the log began."""
    seen: dict[str, datetime] = {}
    for r in sailing_rows:
        if r.get("departure_at") is None or r.get("origin") is None:
            continue
        k = r.get("sailing_key") or sailing_key(r["route_code"], r["origin"], r["departure_at"])  # older parts lack it
        if k not in seen or r["observed_at"] < seen[k]:
            seen[k] = r["observed_at"]
    return seen, (min(seen.values()) if seen else None)


def added_late(key: str, departure: datetime, seen: dict[str, datetime], log_start: datetime | None) -> bool:
    t = seen.get(key)
    return bool(t and log_start and t > log_start + timedelta(hours=1) and departure - t < ADDED_LATE)


def build_labels(day: date, timetable: list[Sailing], events: list[dict], status_rows: list[dict],
                 now: datetime, overrides: dict[str, str] | None = None,
                 seen: dict[str, datetime] | None = None, log_start: datetime | None = None) -> list[dict]:
    overrides, seen = overrides or {}, seen or {}
    definitive, reasons = latest_definitive(events), latest_reason(events)
    flagged = {e["sailing_key"] for e in events if e.get("sailing_key")}
    rows = []
    for s in timetable:
        if s.date != day:
            continue
        departure = departure_of(s)
        out = label_sailing(s, definitive.get(s.key), reasons.get(s.key), s.key in flagged)
        if added_late(s.key, departure, seen, log_start):
            out.update(train=False, exclusion="additional_unscheduled")
        rows.append({
            "source": SOURCE, "sailing_key": s.key, "route_code": s.route, "sailing_date": s.date,
            "departure_local": s.local, "departure_at": departure, "arrival_at": None,
            "actual_departure_at": None, "actual_arrival_at": None,
            "scheduled_leg": leg(s.origin, s.destination), "operated_leg": out["operated_leg"],
            "origin": s.origin, "destination": s.destination,
            "vessel": vessel_at(status_rows, s.route, departure, overrides),
            "outcome": out["label"], "label": out["label"], "reason_detail": reasons.get(s.key),
            "late_reason": None, "delay_minutes": None, "train": out["train"], "exclusion": out["exclusion"],
            "labeller_version": LABELLER_VERSION, "confidence": out["confidence"], "labelled_at": now,
        })
    return rows


def read_live_labels(data_dir: Path) -> list[dict]:
    """Every live label, newest labeller version and labelling run per sailing."""
    best: dict[str, dict] = {}
    for r in read_dataset(Path(data_dir) / "labels_live", LIVE_LABEL_SCHEMA).to_pylist():
        k = (r["labeller_version"], r["labelled_at"])
        if r["sailing_key"] not in best or k > (best[r["sailing_key"]]["labeller_version"], best[r["sailing_key"]]["labelled_at"]):
            best[r["sailing_key"]] = r
    return sorted(best.values(), key=lambda r: r["departure_at"])


def run_labels(now: datetime, data_dir: Path, site_dir: Path) -> dict:
    data_dir = Path(data_dir)
    state_path = data_dir / "state" / "labels_live.json"
    state = load_state(state_path, {"labeller_version": 0, "days": []})
    relabel = state.get("labeller_version") != LABELLER_VERSION
    done = set() if relabel else set(state.get("days", []))

    sailing_rows = read_dataset(data_dir / "sailings", SAILING_SCHEMA).to_pylist()
    start, end = first_full_day(sailing_rows), settled_through(now)
    todo = []
    if start is not None:
        d = start
        while d <= end:
            if d.isoformat() not in done:
                todo.append(d)
            d += timedelta(days=1)

    rows: list[dict] = []
    if todo:
        timetable = build_timetable(sailing_rows)
        events = read_dataset(data_dir / "events", EVENT_SCHEMA).to_pylist()
        if events:
            newest = max(e["parser_version"] for e in events)
            events = [e for e in events if e["parser_version"] == newest]
        status = read_dataset(data_dir / "route_status", ROUTE_STATUS_SCHEMA).to_pylist()
        try:
            overrides = _route_overrides()
        except Exception:  # config missing in some test setups
            overrides = {}
        seen, log_start = first_seen(sailing_rows)
        for d in todo:
            rows += build_labels(d, timetable, events, status, now, overrides, seen, log_start)
            done.add(d.isoformat())
        append_rows(data_dir / "labels_live", rows, LIVE_LABEL_SCHEMA, now)
    save_state(state_path, {"labeller_version": LABELLER_VERSION, "days": sorted(done)})

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["label"]] = counts.get(r["label"], 0) + 1
    summary = {"labeller_version": LABELLER_VERSION, "relabelled_all": relabel,
               "days_labelled": [d.isoformat() for d in todo], "labels": counts,
               "settled_through": end.isoformat()}
    update_health(site_dir, "labels", now, details=summary)
    return summary
