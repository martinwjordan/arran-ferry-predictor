"""Live labels: what happened to each sailing, from notices and timetable removals."""

from datetime import date, datetime, timedelta, timezone

from ferrypredictor import labels as L
from ferrypredictor import storage
from ferrypredictor.collect import ROUTE_STATUS_SCHEMA, SAILING_SCHEMA, sailing_key
from ferrypredictor.parse import EVENT_SCHEMA

UTC = timezone.utc
LOG_START = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
DAY = date(2026, 11, 25)


def dep(hh, mm=0, day=DAY):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=UTC)  # November: UK time is UTC


def sailing(route, origin, destination, departure, change="new", observed=LOG_START):
    return {"sailing_id": 1, "route_code": route, "change_type": change, "observed_at": observed,
            "sailing_date": departure.date(), "origin": origin, "destination": destination,
            "departure_at": departure, "arrival_at": departure + timedelta(minutes=55),
            "rescheduled_departure_at": None, "rescheduled_arrival_at": None, "status": "SCHEDULED",
            "sailing_key": sailing_key(route, origin, departure)}


def event(kind, route, origin, departure, updated, reason="weather", port=None):
    return {"parser_version": 1, "notice_id": f"n{route}", "notice_updated_at": updated, "route_code": route,
            "event_type": kind, "sailing_key": sailing_key(route, origin, departure), "sailing_route": route,
            "sailing_date": departure.date(), "origin": origin, "departure_local": f"{departure:%H:%M}",
            "diversion_port": port, "review_time": None, "reason": reason, "match": "exact",
            "confidence": 1.0, "sentence": "...", "parsed_at": updated}


STATUS = [
    {"route_code": "003", "observed_at": LOG_START, "status": "NORMAL", "effective_status": "NORMAL", "vessels": "MV Caledonian Isles"},
    {"route_code": "301", "observed_at": LOG_START, "status": "NORMAL", "effective_status": "NORMAL", "vessels": "MV Alfred"},
    {"route_code": "301", "observed_at": dep(12), "status": "NORMAL", "effective_status": "NORMAL", "vessels": "MV Glen Sannox"},
]

SAILINGS = [
    sailing("003", "Ardrossan", "Brodick", dep(7)),     # cancelled by notice
    sailing("003", "Brodick", "Ardrossan", dep(8, 20)),  # cancelled, then reinstated
    sailing("003", "Ardrossan", "Brodick", dep(9, 45)),  # diverted to Troon
    sailing("003", "Brodick", "Ardrossan", dep(11, 5)),  # at risk, then removed: cancelled
    sailing("003", "Ardrossan", "Brodick", dep(12, 30)),  # removed days ahead, no notice: planned
    sailing("003", "Brodick", "Ardrossan", dep(13, 55)),  # removed, no notice, no reason
    sailing("003", "Ardrossan", "Brodick", dep(15, 20)),  # at risk, ran
    sailing("003", "Brodick", "Ardrossan", dep(16, 40)),  # nothing said, ran
    sailing("301", "Troon", "Brodick", dep(10, 15)),     # Alfred
    sailing("301", "Troon", "Brodick", dep(14, 10)),     # Glen Sannox by then
    sailing("301", "Troon", "Brodick", dep(11, 30), observed=dep(6)),  # relief sailing added that morning
    sailing("003", "Brodick", "Ardrossan", dep(11, 5), "removed", dep(8)),
    sailing("003", "Ardrossan", "Brodick", dep(12, 30), "removed", dep(12, 30) - timedelta(days=4)),
    sailing("003", "Brodick", "Ardrossan", dep(13, 55), "removed", dep(10)),
]

EVENTS = [
    event("cancelled", "003", "Ardrossan", dep(7), dep(6)),
    event("at_risk", "003", "Ardrossan", dep(7), dep(9)),  # a later "at risk" never undoes a cancellation
    event("cancelled", "003", "Brodick", dep(8, 20), dep(6), reason="operational"),
    event("reinstated", "003", "Brodick", dep(8, 20), dep(7)),
    event("diverted", "003", "Ardrossan", dep(9, 45), dep(6), port="Troon"),
    event("at_risk", "003", "Brodick", dep(11, 5), dep(7)),
    event("at_risk", "003", "Ardrossan", dep(15, 20), dep(7)),
]


def labels_by_time(now=datetime(2026, 11, 27, 9, 0, tzinfo=UTC)):
    from ferrypredictor.parse import build_timetable
    seen, start = L.first_seen(SAILINGS)
    rows = L.build_labels(DAY, build_timetable(SAILINGS), EVENTS, STATUS, now, {}, seen, start)
    return {(r["route_code"], r["departure_local"]): r for r in rows}


def test_notice_beats_timetable_and_the_latest_definitive_word_wins():
    by = labels_by_time()
    assert (by[("003", "07:00")]["label"], by[("003", "07:00")]["confidence"]) == ("cancelled_weather", "notice")
    assert by[("003", "08:20")]["label"] == "operated"  # reinstated after the cancellation
    d = by[("003", "09:45")]
    assert d["label"] == "diverted" and d["operated_leg"] == "Troon-Brodick" and d["scheduled_leg"] == "Ardrossan-Brodick"


def test_removal_before_departure_is_a_cancellation_unless_planned_well_ahead():
    by = labels_by_time()
    r = by[("003", "11:05")]
    assert (r["label"], r["confidence"], r["train"]) == ("cancelled_weather", "removal", True)
    planned = by[("003", "12:30")]
    assert (planned["label"], planned["train"], planned["exclusion"]) == ("planned_change", False, "planned_timetable_change")
    unknown = by[("003", "13:55")]
    assert (unknown["label"], unknown["train"], unknown["exclusion"]) == ("cancelled_unknown", False, "cancellation_reason_unknown")


def test_ran_with_or_without_a_warning_and_late_additions_are_left_out():
    by = labels_by_time()
    assert (by[("003", "15:20")]["label"], by[("003", "15:20")]["confidence"]) == ("operated", "at_risk")
    assert (by[("003", "16:40")]["label"], by[("003", "16:40")]["confidence"]) == ("operated", "timetable")
    relief = by[("301", "11:30")]
    assert relief["train"] is False and relief["exclusion"] == "additional_unscheduled"


def test_vessel_as_it_stood_at_departure():
    by = labels_by_time()
    assert by[("003", "07:00")]["vessel"] == "CALEDONIAN ISLES"
    assert by[("301", "10:15")]["vessel"] == "ALFRED" and by[("301", "14:10")]["vessel"] == "GLEN SANNOX"


def test_days_are_labelled_once_after_they_settle(tmp_path):
    data, site = tmp_path / "data", tmp_path / "site"
    storage.append_rows(data / "sailings", SAILINGS, SAILING_SCHEMA, LOG_START)
    storage.append_rows(data / "events", EVENTS, EVENT_SCHEMA, LOG_START)
    storage.append_rows(data / "route_status", STATUS, ROUTE_STATUS_SCHEMA, LOG_START)

    early = L.run_labels(datetime(2026, 11, 26, 22, 0, tzinfo=UTC), data, site)  # the 25th hasn't settled
    assert early["days_labelled"] == ["2026-11-21", "2026-11-22", "2026-11-23", "2026-11-24"]
    first = L.run_labels(datetime(2026, 11, 27, 0, 30, tzinfo=UTC), data, site)
    assert first["days_labelled"] == ["2026-11-25"]
    assert first["labels"]["cancelled_weather"] == 2
    again = L.run_labels(datetime(2026, 11, 27, 0, 50, tzinfo=UTC), data, site)
    assert again["days_labelled"] == []
    live = L.read_live_labels(data)
    assert len(live) == len({s["sailing_key"] for s in SAILINGS}) and {r["source"] for r in live} == {"live"}
    assert L.first_full_day(SAILINGS) == date(2026, 11, 21)  # the log began mid-morning on the 20th
