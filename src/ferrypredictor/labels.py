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
3. Otherwise operated: confidence "timetable", or "at_risk" when a notice had flagged it.

Diversion days (labeller version 2). When CalMac runs a route via Troon to a listed timetable
("MV Caledonian Isles will operate via Troon to the following timetable:"), the times rarely match the
scheduled ones, CalMac removes some replaced sailings and leaves others listed "at risk". The labeller
follows CalMac's own FOI records for such days:

- A listed diversion departure counts as run if it was still in the latest version, issued before it
  was due, of a notice that listed it, and no notice dropped it before it was due.
- Each run departure is paired with one scheduled sailing on the diverted route in the same direction
  (Troon stands in for Ardrossan): closest in time first, within 150 minutes, keeping the order.
  Paired sailings are diverted (operated leg via Troon), confidence "diversion_timetable".
- Scheduled sailings due between the first and last run diversion departure that were not paired,
  and were removed or flagged at risk, were not run: cancelled, by the notice's reason, confidence
  "diversion_timetable". Sailings before or after that span keep their ordinary label (on 23 November
  the 18:00 from Ardrossan ran before the 19:00 from Brodick went to Troon).
- A notice's own "cancelled" or "reinstated" for a sailing still wins over pairing.
- If a run departure cannot be paired, the unpaired at-risk or removed sailings in that span are kept
  but marked diversion_day_unresolved: not trained on or scored.

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

from .collect import NOTICE_SCHEMA, ROUTE_STATUS_SCHEMA, SAILING_SCHEMA, sailing_key
from .foi import LABEL_SCHEMA
from .health import update_health
from . import checks
from .parse import DIVERSION, EVENT_SCHEMA, UK, Sailing, build_timetable, diversion_timetables
from .predict import _route_overrides, operating_vessels
from .storage import append_rows, load_state, read_dataset, save_state

LABELLER_VERSION = 3  # 2: diversion-day pairing (25 Nov 2026); 3: removals need a notice, quarantine (26 Nov 2026)
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
NOT_SCORED = ("planned_timetable_change", "additional_unscheduled", "diversion_day_unresolved",
              "removal_unconfirmed", "data_fault")  # left out of scoring
CONFIDENT = ("notice", "timetable", "diversion_timetable")
PAIR_WINDOW = 150  # minutes between a diversion departure and the scheduled sailing it replaces


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
        elif not s.removal_confirmed:
            # Dropped from CalMac's feed with no notice behind it, or in a mass drop: we don't know (checks.py)
            label, exclusion = "unknown", "removal_unconfirmed"
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


def _at(day: date, local: str) -> datetime:
    return datetime(day.year, day.month, day.day, int(local[:2]), int(local[3:]), tzinfo=UK)


def diversion_runs(notices: list[dict], day: date) -> dict[str, list[dict]]:
    """Per diverted route, the listed diversion departures on this day that went ahead.

    Each is {origin, local, minutes, reason}. Run means: still listed in the latest pre-departure
    version of at least one notice, and not dropped before departure by any notice that had listed it.
    """
    versions: dict[str, list[tuple[datetime, list[dict], str | None]]] = {}
    for n in notices:
        if n.get("notice_type") not in (None, "SAILING") or n.get("updated_at") is None:
            continue
        items = [d for d in diversion_timetables(n) if d["day"] == day]
        versions.setdefault(n["notice_id"], []).append((n["updated_at"], items, n.get("reason")))
    candidates: dict[tuple, str | None] = {}
    for vs in versions.values():
        vs.sort(key=lambda v: v[0])
        for _, items, reason in vs:
            for d in items:
                candidates.setdefault((d["route"], d["origin"], d["local"]), reason)
    out: dict[str, list[dict]] = {}
    for (route, origin, local), reason in candidates.items():
        due = _at(day, local)
        support = dropped = False
        for vs in versions.values():
            before = [v for v in vs if v[0] < due]
            if not before:
                continue
            listed = [(route, origin, local) in {(d["route"], d["origin"], d["local"]) for d in v[1]} for v in before]
            if listed[-1]:
                support = True
            elif any(listed):
                dropped = True
        if support and not dropped:
            out.setdefault(route, []).append({"origin": origin, "local": local, "reason": reason,
                                              "minutes": int(local[:2]) * 60 + int(local[3:])})
    for v in out.values():
        v.sort(key=lambda d: d["minutes"])
    return out


def pair_diversions(route: str, runs: list[dict], sailings: list[Sailing]) -> tuple[dict[str, dict], list[dict]]:
    """Pair run diversion departures with the scheduled sailings they replaced: closest first, same
    direction, within PAIR_WINDOW, never crossing an earlier pair. Returns (pairs by sailing key, unpaired runs)."""
    divert_to, replaces = DIVERSION.get(route, (None, None))
    options = []
    for i, d in enumerate(runs):
        origin = replaces if d["origin"] == divert_to else d["origin"]
        for s in sailings:
            gap = abs(s.minutes - d["minutes"])
            if s.origin == origin and gap <= PAIR_WINDOW:
                options.append((gap, s.minutes, i, s))
    pairs: dict[str, dict] = {}
    used: set[int] = set()
    for _, _, i, s in sorted(options, key=lambda o: o[:2]):
        if i in used or s.key in pairs:
            continue
        d = runs[i]
        if any(p["origin_sched"] == s.origin and (p["minutes"] - d["minutes"]) * (p["sailing"].minutes - s.minutes) < 0
               for p in pairs.values()):
            continue  # would cross an existing pair in the same direction
        used.add(i)
        pairs[s.key] = {**d, "sailing": s, "origin_sched": s.origin}
    return pairs, [d for i, d in enumerate(runs) if i not in used]


def apply_diversions(day: date, timetable: list[Sailing], labelled: dict[str, dict], notices: list[dict],
                     definitive: dict[str, dict], reasons: dict[str, str], flagged: set[str]) -> None:
    """Overwrite labels for sailings on a diverted route's day (see module docstring)."""
    for route, runs in diversion_runs(notices, day).items():
        divert_to, replaces = DIVERSION.get(route, (None, None))
        sailings = [s for s in timetable if s.route == route and s.date == day and s.key in labelled
                    and labelled[s.key]["exclusion"] != "planned_timetable_change"
                    and (definitive.get(s.key) or {}).get("event_type") not in ("cancelled", "reinstated")]
        pairs, unpaired = pair_diversions(route, runs, sailings)
        lo, hi = runs[0]["minutes"], runs[-1]["minutes"]
        for s in sailings:
            out = labelled[s.key]
            if s.key in pairs:
                port = divert_to if s.origin == replaces else s.origin
                dest = s.destination if s.origin == replaces else divert_to
                out.update(label="diverted", operated_leg=leg(port, dest), train=True, exclusion=None,
                           confidence="notice" if out["confidence"] == "notice" else "diversion_timetable")
            elif lo <= s.minutes <= hi and (s.key in flagged or s.removed) and out["confidence"] != "notice":
                if unpaired:
                    out.update(train=False, exclusion="diversion_day_unresolved", confidence="diversion_unresolved")
                else:
                    label = cancelled_label(reasons.get(s.key) or runs[0]["reason"])
                    out.update(label=label, operated_leg=None, confidence="diversion_timetable",
                               train=label != "cancelled_unknown",
                               exclusion="cancellation_reason_unknown" if label == "cancelled_unknown" else None)


def build_labels(day: date, timetable: list[Sailing], events: list[dict], status_rows: list[dict],
                 now: datetime, overrides: dict[str, str] | None = None,
                 seen: dict[str, datetime] | None = None, log_start: datetime | None = None,
                 notices: list[dict] | None = None) -> list[dict]:
    overrides, seen = overrides or {}, seen or {}
    definitive, reasons = latest_definitive(events), latest_reason(events)
    flagged = {e["sailing_key"] for e in events if e.get("sailing_key")}
    day_sailings = [s for s in timetable if s.date == day]
    labelled = {s.key: label_sailing(s, definitive.get(s.key), reasons.get(s.key), s.key in flagged) for s in day_sailings}
    if notices:
        apply_diversions(day, day_sailings, labelled, notices, definitive, reasons, flagged)
    rows = []
    for s in day_sailings:
        departure = departure_of(s)
        out = labelled[s.key]
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


def quarantine(rows: list[dict], timetable: list[Sailing], windows: list[dict]) -> None:
    """Leave out of training and scoring any sailing due, or removed, while the data was known to be faulty."""
    removed_at = {s.key: s.removed_at for s in timetable if s.removed}
    for r in rows:
        if checks.quarantined(r.get("departure_at"), windows) or checks.quarantined(removed_at.get(r["sailing_key"]), windows):
            r["exclusion"], r["train"] = "data_fault", False


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
        events = read_dataset(data_dir / "events", EVENT_SCHEMA).to_pylist()
        if events:
            newest = max(e["parser_version"] for e in events)
            events = [e for e in events if e["parser_version"] == newest]
        timetable = checks.confirm_removals(build_timetable(sailing_rows), events)
        status = read_dataset(data_dir / "route_status", ROUTE_STATUS_SCHEMA).to_pylist()
        try:
            overrides = _route_overrides()
        except Exception:  # config missing in some test setups
            overrides = {}
        notices = read_dataset(data_dir / "notices", NOTICE_SCHEMA).to_pylist()
        seen, log_start = first_seen(sailing_rows)
        for d in todo:
            rows += build_labels(d, timetable, events, status, now, overrides, seen, log_start, notices)
            done.add(d.isoformat())
        quarantine(rows, timetable, checks.quarantine_windows(data_dir))
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
