"""Checks and balances: no single source is believed on its own, and bad data never reaches the model.

Written after 26 November 2026. At 14:47 CalMac's API started returning no sailings for any route or date,
while route status and notices still came through. The collector logged every future sailing as removed,
predict showed them all at 0%, the page and the TV showed every sailing cancelled, and none of it was true:
CalMac had cancelled one sailing (Friday's 07:00 from Ardrossan). Three layers stop that happening again.

1. Corroboration. A sailing dropping out of CalMac's timetable feed is a cancellation only if CalMac's
   notices back it up: the parser has a cancelled, diverted or at-risk event for that sailing. Removals that
   come in a mass batch (12 or more sailings, or 3 or more days, on one route in one poll) are never
   confirmed, notice or not: CalMac cancels sailings, it doesn't empty days in bulk without a timetable
   change. An unconfirmed removal is shown as "missing from CalMac's feed", keeps its forecast chance,
   and is neither trained on nor scored. (The collector itself treats an empty route as a feed gap.)

2. Sanity gate (run after predict). Checks the feed, the timetable, the predictions, the forecasts and the
   livestock wording against simple invariants. Writes site/sanity.json; the page and Home Assistant show a
   "data problem" banner on a failure, and the workflow fails so three in a row raise a GitHub issue.

3. Quarantine. While the gate is failing, the window is recorded (data/state/quarantine.json), and windows can
   be added by hand (config/quarantine.yaml). Sailings due in a quarantine window, and removals or predictions
   made in one, are left out of labels for training and out of the scorecard.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from . import cargo, parse
from .config import config_dir
from .health import update_health
from .parse import UK, Sailing
from .storage import load_state, save_state

MASS_COUNT = 12
MASS_DAYS = 3
CORROBORATING = {"cancelled", "at_risk", "at_risk_diversion", "diverted"}
COLLECT_STALE = timedelta(minutes=70)
PREDICT_STALE = timedelta(minutes=90)
FORECAST_WARN, FORECAST_FAIL = timedelta(hours=18), timedelta(hours=36)
RANK = {"ok": 0, "warn": 1, "fail": 2}
QUARANTINE_STATE = "quarantine.json"


# 1. Corroboration -------------------------------------------------------------------------------------------

def newest_events(events: list[dict]) -> list[dict]:
    if not events:
        return []
    newest = max(e["parser_version"] for e in events)
    return [e for e in events if e["parser_version"] == newest]


def mass_batches(timetable: list[Sailing]) -> dict[tuple[str, datetime], int]:
    """(route, removed_at) -> count, for removals that came in a suspiciously large batch."""
    groups: dict[tuple[str, datetime], list[Sailing]] = {}
    for s in timetable:
        if s.removed and s.removed_at is not None:
            groups.setdefault((s.route, s.removed_at), []).append(s)
    return {k: len(v) for k, v in groups.items() if len(v) >= MASS_COUNT or len({s.date for s in v}) >= MASS_DAYS}


def confirm_removals(timetable: list[Sailing], events: list[dict]) -> list[Sailing]:
    """Set removal_confirmed on each removed sailing: backed by a notice, and not part of a mass batch."""
    backed = {e["sailing_key"] for e in newest_events(events) if e.get("sailing_key") and e["event_type"] in CORROBORATING}
    mass = mass_batches(timetable)
    return [replace(s, removal_confirmed=s.removed and s.key in backed and (s.route, s.removed_at) not in mass)
            for s in timetable]


def timetable(sail_rows: list[dict], events: list[dict]) -> list[Sailing]:
    """parse.build_timetable, with each removal checked against the notices."""
    return confirm_removals(parse.build_timetable(sail_rows), events)


# 3. Quarantine ----------------------------------------------------------------------------------------------

def quarantine_windows(data_dir: Path) -> list[dict]:
    """Every quarantine window, by hand (config/quarantine.yaml) and automatic (failed sanity gate)."""
    import yaml

    out = []
    path = config_dir() / "quarantine.yaml"
    if path.exists():
        for w in (yaml.safe_load(path.read_text()) or {}).get("windows", []) or []:
            out.append({"start": _ts(w["start"]), "end": _ts(w.get("end")), "reason": w.get("reason", "by hand")})
    for w in load_state(Path(data_dir) / "state" / QUARANTINE_STATE, []):
        out.append({"start": _ts(w["start"]), "end": _ts(w.get("end")), "reason": w.get("reason", "sanity gate")})
    return out


def _ts(v) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


def quarantined(when: datetime | None, windows: list[dict]) -> str | None:
    """The reason, if `when` falls in a quarantine window (an open window runs to now)."""
    if when is None:
        return None
    for w in windows:
        if w["start"] <= when and (w["end"] is None or when <= w["end"]):
            return w["reason"]
    return None


def _update_quarantine(data_dir: Path, now: datetime, status: str, reason: str) -> list[dict]:
    path = Path(data_dir) / "state" / QUARANTINE_STATE
    windows = load_state(path, [])
    open_w = windows[-1] if windows and windows[-1].get("end") is None else None
    if status == "fail" and open_w is None:
        windows.append({"start": now.isoformat(), "end": None, "reason": reason})
    elif status != "fail" and open_w is not None:
        open_w["end"] = now.isoformat()
    save_state(path, windows)
    return windows


# 2. Sanity gate ---------------------------------------------------------------------------------------------

def _check(out: list, cid: str, status: str, message: str) -> None:
    out.append({"id": cid, "status": status, "message": message})


def sanity(now: datetime, data_dir: Path, site_dir: Path) -> dict:
    from .collect import NOTICE_SCHEMA, SAILING_SCHEMA
    from .site import active_versions
    from .storage import read_dataset

    from .config import load_routes

    data_dir, site_dir = Path(data_dir), Path(site_dir)
    checks: list[dict] = []
    routes = load_routes()
    name = {r.code: r.name.replace(" - ", " to ") for r in routes}
    health = {f.stem: json.loads(f.read_text()) for f in sorted((site_dir / "health").glob("*.json"))}

    # Feed: the collector ran recently, and CalMac returned sailings for every route
    col = health.get("collect", {})
    last = _ts(col.get("last_success"))
    if last is None or now - last > COLLECT_STALE:
        _check(checks, "collect_fresh", "fail", f"CalMac hasn't been read since {last:%H:%M %d %b}." if last else "CalMac has never been read.")
    gaps = (col.get("details") or {}).get("sailings_feed_empty") or []
    if gaps:
        _check(checks, "feed_empty", "fail", f"CalMac's timetable feed is returning no sailings "
               f"({', '.join(name.get(g, g) for g in gaps)}); showing the last known timetable.")

    # Timetable: every route has sailings in the next three days, and no mass removals are recent
    events = read_dataset(data_dir / "events", parse.EVENT_SCHEMA).to_pylist()
    tt = timetable(read_dataset(data_dir / "sailings", SAILING_SCHEMA).to_pylist(), events)
    today = now.astimezone(UK).date()
    for r in routes:
        alive = [s for s in tt if s.route == r.code and not s.removed
                 and now < datetime.fromisoformat(s.key.split("|")[2]) < now + timedelta(days=3)]
        if not alive:
            _check(checks, f"timetable_{r.code}", "fail", f"No sailings to come for {name[r.code]} in the next three days.")
    recent_mass = {k: n for k, n in mass_batches(tt).items() if now - k[1] < timedelta(hours=48)}
    for (route, at), n in recent_mass.items():
        _check(checks, f"mass_removal_{route}", "warn", f"CalMac's feed dropped {n} {name.get(route, route)} sailings at once at "
               f"{at.astimezone(UK):%H:%M %d %b}; not treated as cancellations unless notices say so.")

    # Predictions: fresh, and nothing shown as cancelled without CalMac saying so
    pj = site_dir / "predictions.json"
    if pj.exists():
        p = json.loads(pj.read_text())
        gen = _ts(p.get("generated_at"))
        if gen is None or now - gen > PREDICT_STALE:
            _check(checks, "predict_fresh", "fail", "Chances haven't been worked out for over 90 minutes.")
        backed = {e["sailing_key"] for e in newest_events(events) if e["event_type"] == "cancelled"}
        removed_ok = {s.key for s in tt if s.removal_confirmed}
        days = {today.isoformat(), (today + timedelta(days=1)).isoformat()}
        soon = [s for s in p.get("sailings", []) if s.get("date") in days and not s.get("departed")]
        zero = [s for s in soon if s.get("calmac") == "cancelled"]
        unbacked = [s for s in zero if s["key"] not in backed and s["key"] not in removed_ok]
        if unbacked:
            _check(checks, "cancelled_unbacked", "fail", f"{len(unbacked)} sailings shown as cancelled without a CalMac notice.")
        if soon and len(zero) / len(soon) > 0.6 and len(zero) > len(backed & {s["key"] for s in zero}):
            _check(checks, "cancelled_share", "fail", f"{len(zero)} of {len(soon)} sailings today and tomorrow shown as cancelled.")
        runs = [_ts(v) for v in (p.get("runs") or {}).values() if v]
        if runs:
            age = now - max(runs)
            if age > FORECAST_FAIL:
                _check(checks, "forecast_fresh", "fail", f"The newest weather forecast is {age.total_seconds() / 3600:.0f} hours old.")
            elif age > FORECAST_WARN:
                _check(checks, "forecast_fresh", "warn", f"The newest weather forecast is {age.total_seconds() / 3600:.0f} hours old.")
    else:
        _check(checks, "predict_fresh", "fail", "No chances have been worked out yet.")

    # Livestock and timber: every sentence that mentions them is understood
    live = active_versions(read_dataset(data_dir / "notices", NOTICE_SCHEMA).to_pylist(), now)
    for n in live.values():
        for sentence in cargo._sentences(n.get("detail") or ""):
            if cargo.CARGO_RE.search(sentence) and cargo.classify(sentence) is None:
                _check(checks, "livestock_wording", "warn", f'Livestock/timber wording not understood ({name.get(n["route_code"], n["route_code"])}): "{sentence[:160]}"')

    status = max((c["status"] for c in checks), key=RANK.get, default="ok")
    fails = [c["message"] for c in checks if c["status"] == "fail"]
    windows = _update_quarantine(data_dir, now, status, "; ".join(fails)[:300] or "sanity gate")
    out = {"checked_at": now.isoformat(timespec="seconds"), "status": status, "checks": checks,
           "quarantine_open": bool(windows and windows[-1].get("end") is None)}
    (site_dir / "sanity.json").write_text(json.dumps(out, indent=1))
    # Home Assistant reads predictions.json, so the verdict goes there too
    if pj.exists():
        p = json.loads(pj.read_text())
        p["sanity"] = {"status": status, "messages": [c["message"] for c in checks if c["status"] != "ok"][:5]}
        pj.write_text(json.dumps(p, indent=1, default=str))
    return out


def run_sanity(now: datetime, data_dir: Path, site_dir: Path) -> dict:
    out = sanity(now, data_dir, site_dir)
    update_health(site_dir, "sanity", now, details={"status": out["status"], "checks": len(out["checks"])})
    return {"status": out["status"], "problems": [c["message"] for c in out["checks"]]}


def gate(site_dir: Path) -> int:
    """Exit code for the workflow's last step: 1 when the latest sanity check failed."""
    path = Path(site_dir) / "sanity.json"
    if not path.exists():
        return 0
    out = json.loads(path.read_text())
    if out.get("status") == "fail":
        for c in out["checks"]:
            if c["status"] == "fail":
                print(f"sanity: {c['message']}")
        return 1
    return 0
