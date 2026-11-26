"""Checks and balances (checks.py), built around the failure of 26 November 2026: CalMac's timetable feed went
empty at 14:47 and every future sailing was shown as cancelled."""

import json
from datetime import datetime, timedelta, timezone

from ferrypredictor import checks, collect, parse
from ferrypredictor.collect import sailing_key
from ferrypredictor.health import update_health
from ferrypredictor.storage import append_rows

UTC = timezone.utc
NOW = datetime(2026, 11, 26, 15, 7, tzinfo=UTC)
TIMES = ["07:00", "08:20", "09:45", "11:05", "12:30", "13:55", "15:20", "16:40", "18:00", "19:20"]


def sailing(day, hhmm, change="new", observed=datetime(2026, 11, 23, 12, 0, tzinfo=UTC), route="003"):
    origin = "Ardrossan" if TIMES.index(hhmm) % 2 == 0 else "Brodick"
    dep = datetime(2026, 11, day, int(hhmm[:2]), int(hhmm[3:]), tzinfo=UTC)
    return {"sailing_id": 1, "route_code": route, "change_type": change, "observed_at": observed,
            "sailing_date": dep.date(), "origin": origin, "destination": "Brodick" if origin != "Brodick" else "Ardrossan",
            "departure_at": dep, "arrival_at": dep + timedelta(minutes=55), "rescheduled_departure_at": None,
            "rescheduled_arrival_at": None, "status": "SCHEDULED", "sailing_key": sailing_key(route, origin, dep)}


def event(row, kind="at_risk"):
    return {"parser_version": 2, "notice_id": "n", "notice_updated_at": NOW, "route_code": row["route_code"],
            "event_type": kind, "sailing_key": row["sailing_key"], "sailing_route": row["route_code"],
            "sailing_date": row["sailing_date"], "origin": row["origin"], "departure_local": "",
            "diversion_port": None, "review_time": None, "reason": "weather", "match": "exact", "confidence": 1.0,
            "sentence": "...", "parsed_at": NOW}


def week():
    return [sailing(d, t, route=r) for r in ("003", "301") for d in range(26, 30) for t in TIMES]


def test_single_removal_with_a_notice_is_confirmed_and_without_one_is_not():
    rows = week()
    a, b = rows[12], rows[13]
    rows += [{**a, "change_type": "removed", "observed_at": NOW}, {**b, "change_type": "removed", "observed_at": NOW}]
    tt = {s.key: s for s in checks.timetable(rows, [event(a)])}
    assert tt[a["sailing_key"]].removal_confirmed
    assert tt[b["sailing_key"]].removed and not tt[b["sailing_key"]].removal_confirmed


def test_mass_removal_is_never_confirmed_even_with_notices():
    """14:47, 26 Nov: everything vanished at once while notices had sailings at risk."""
    rows = week()
    gone = [r for r in rows if r["departure_at"] > NOW]
    rows += [{**r, "change_type": "removed", "observed_at": NOW} for r in gone]
    tt = checks.timetable(rows, [event(r) for r in gone])
    assert all(s.removed and not s.removal_confirmed for s in tt if s.key in {r["sailing_key"] for r in gone})
    assert sum(checks.mass_batches(parse.build_timetable(rows)).values()) == len(gone)


def test_quarantine_window_matching():
    w = [{"start": datetime(2026, 11, 26, 14, 47, tzinfo=UTC), "end": datetime(2026, 11, 27, tzinfo=UTC), "reason": "feed"}]
    assert checks.quarantined(datetime(2026, 11, 26, 16, tzinfo=UTC), w) == "feed"
    assert checks.quarantined(datetime(2026, 11, 27, 1, tzinfo=UTC), w) is None
    assert checks.quarantined(None, w) is None
    open_w = [{**w[0], "end": None}]
    assert checks.quarantined(datetime(2026, 12, 25, tzinfo=UTC), open_w) == "feed"


def _setup(tmp_path, rows, events, feed_empty=(), predictions=None, collected=NOW):
    data, site = tmp_path / "data", tmp_path / "site"
    append_rows(data / "sailings", rows, collect.SAILING_SCHEMA, NOW)
    append_rows(data / "events", events, parse.EVENT_SCHEMA, NOW)
    append_rows(data / "notices", [], collect.NOTICE_SCHEMA, NOW)
    details = {"sailings_tracked": 0 if feed_empty else len(rows)}
    if feed_empty:
        details["sailings_feed_empty"] = list(feed_empty)
    update_health(site, "collect", collected, details=details)
    site.mkdir(parents=True, exist_ok=True)
    p = predictions if predictions is not None else {
        "generated_at": NOW.isoformat(), "runs": {"gfs_seamless": (NOW - timedelta(hours=3)).isoformat()}, "sailings": []}
    (site / "predictions.json").write_text(json.dumps(p))
    return data, site


def test_sanity_fails_on_tonights_failure_and_opens_then_closes_quarantine(tmp_path):
    rows = week()
    gone = [r for r in rows if r["departure_at"] > NOW]
    bad_rows = rows + [{**r, "change_type": "removed", "observed_at": NOW} for r in gone]
    shown = [{"key": r["sailing_key"], "date": r["sailing_date"].isoformat(), "departed": False, "calmac": "cancelled"}
             for r in gone if r["sailing_date"].day in (26, 27)]
    preds = {"generated_at": NOW.isoformat(), "runs": {"gfs_seamless": NOW.isoformat()}, "sailings": shown}
    data, site = _setup(tmp_path, bad_rows, [], feed_empty=["003", "301"], predictions=preds)
    out = checks.sanity(NOW, data, site)
    ids = {c["id"]: c["status"] for c in out["checks"]}
    assert out["status"] == "fail"
    assert ids["feed_empty"] == "fail" and ids["timetable_003"] == "fail" and ids["cancelled_unbacked"] == "fail"
    assert ids["mass_removal_003"] == "warn" and ids["timetable_301"] == "fail"
    assert out["quarantine_open"]
    assert json.loads((site / "predictions.json").read_text())["sanity"]["status"] == "fail"
    assert checks.gate(site) == 1

    # Next run: feed back, nothing shown cancelled
    later = NOW + timedelta(minutes=20)
    (site / "predictions.json").write_text(json.dumps({"generated_at": later.isoformat(),
                                                     "runs": {"gfs_seamless": later.isoformat()}, "sailings": []}))
    update_health(site, "collect", later, details={"sailings_tracked": 30})
    (data / "sailings").mkdir(exist_ok=True)
    for f in (data / "sailings").rglob("*.parquet"):
        f.unlink()
    append_rows(data / "sailings", rows, collect.SAILING_SCHEMA, later)
    out = checks.sanity(later, data, site)
    assert out["status"] == "ok", out["checks"]
    assert not out["quarantine_open"]
    auto = [w for w in checks.quarantine_windows(data) if w["start"] == NOW]  # config/quarantine.yaml adds its own
    assert len(auto) == 1 and auto[0]["end"] == later
    assert checks.gate(site) == 0


def test_sanity_flags_stale_collect_and_forecast(tmp_path):
    rows = week()
    preds = {"generated_at": NOW.isoformat(), "runs": {"gfs_seamless": (NOW - timedelta(hours=20)).isoformat()}, "sailings": []}
    data, site = _setup(tmp_path, rows, [], predictions=preds, collected=NOW - timedelta(hours=2))
    ids = {c["id"]: c["status"] for c in checks.sanity(NOW, data, site)["checks"]}
    assert ids["collect_fresh"] == "fail" and ids["forecast_fresh"] == "warn"
