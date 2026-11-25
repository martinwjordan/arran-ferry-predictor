"""Diversion days: CalMac's Troon timetable paired with the scheduled Ardrossan sailings, as CalMac's FOI records do.

Built on the real notices of 23 and 24 November 2026 (tests/fixtures/calmac/notices_2026-11-24.json).
What happened on the 24th: Caledonian Isles ran Troon 07:00, Brodick 09:10, Troon 11:30, Brodick 13:20,
Troon 17:00, then Brodick 19:00 to Ardrossan. CalMac removed the 08:20, 09:45, 11:05 and 12:30 from the
feed and left the rest listed "at risk".
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ferrypredictor import labels as L
from ferrypredictor.collect import sailing_key
from ferrypredictor.parse import build_timetable

UTC = timezone.utc
FIX = Path(__file__).parent / "fixtures" / "calmac"
LOG_START = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
D23, D24 = date(2026, 11, 23), date(2026, 11, 24)
NOW = datetime(2026, 11, 26, 1, 0, tzinfo=UTC)
ARD = ["07:00", "09:45", "12:30", "15:20", "18:00"]
BRO = ["08:20", "11:05", "13:55", "16:40", "19:20"]


def at(day, hhmm):
    return datetime(day.year, day.month, day.day, int(hhmm[:2]), int(hhmm[3:]), tzinfo=UTC)  # November: UK = UTC


def notices():
    out = []
    for n in json.loads((FIX / "notices_2026-11-24.json").read_text()):
        for k in ("updated_at", "first_seen_at", "start_at", "end_at", "next_review_at"):
            n[k] = datetime.fromisoformat(n[k]) if n.get(k) else None
        out.append(n)
    return out


def row(origin, dest, departure, change="new", observed=LOG_START):
    return {"sailing_id": 1, "route_code": "003", "change_type": change, "observed_at": observed,
            "sailing_date": departure.date(), "origin": origin, "destination": dest, "departure_at": departure,
            "arrival_at": departure + timedelta(minutes=55), "rescheduled_departure_at": None,
            "rescheduled_arrival_at": None, "status": "SCHEDULED", "sailing_key": sailing_key("003", origin, departure)}


def sailings(day, removed=()):
    rows = [row("Ardrossan", "Brodick", at(day, t)) for t in ARD] + [row("Brodick", "Ardrossan", at(day, t)) for t in BRO]
    for t in removed:
        origin, dest = ("Ardrossan", "Brodick") if t in ARD else ("Brodick", "Ardrossan")
        rows.append(row(origin, dest, at(day, t), "removed", at(day, t) - timedelta(hours=3)))
    return rows


def flags(day, times, kind="at_risk", reason="weather", updated=None):
    out = []
    for t in times:
        origin = "Ardrossan" if t in ARD else "Brodick"
        out.append({"parser_version": 2, "notice_id": "n003", "notice_updated_at": updated or at(day, "05:00"),
                    "route_code": "003", "event_type": kind, "sailing_key": sailing_key("003", origin, at(day, t)),
                    "sailing_route": "003", "sailing_date": day, "origin": origin, "departure_local": t,
                    "diversion_port": "Troon" if kind == "diverted" else None, "review_time": None, "reason": reason,
                    "match": "exact", "confidence": 1.0, "sentence": "...", "parsed_at": NOW})
    return out


def label(day, rows, events, ns):
    out = L.build_labels(day, build_timetable(rows), events, [], NOW, {}, *L.first_seen(rows), notices=ns)
    return {r["departure_local"]: r for r in out}


def test_24_november_matches_calmacs_own_convention():
    events = flags(D24, ARD + BRO) + flags(D24, ["07:00"], kind="diverted")
    by = label(D24, sailings(D24, removed=["08:20", "09:45", "11:05", "12:30"]), events, notices())
    got = {t: (r["label"], r["operated_leg"]) for t, r in by.items()}
    assert got == {
        "07:00": ("diverted", "Troon-Brodick"),
        "08:20": ("diverted", "Brodick-Troon"),   # Brodick 09:10 to Troon
        "09:45": ("cancelled_weather", None),
        "11:05": ("cancelled_weather", None),
        "12:30": ("diverted", "Troon-Brodick"),   # Troon 11:30
        "13:55": ("diverted", "Brodick-Troon"),   # Brodick 13:20
        "15:20": ("cancelled_weather", None),
        "16:40": ("cancelled_weather", None),
        "18:00": ("diverted", "Troon-Brodick"),   # Troon 17:00
        "19:20": ("operated", "Brodick-Ardrossan"),  # after the Troon timetable: the 19:00 went to Ardrossan
    }
    assert by["07:00"]["confidence"] == "notice" and by["12:30"]["confidence"] == "diversion_timetable"
    assert all(r["train"] for r in by.values())


def test_sailing_before_an_evening_diversion_keeps_its_label():
    # 23 November: service ran to Ardrossan until the 18:55 arrival, then the 19:00 from Brodick went to Troon.
    by = label(D23, sailings(D23), flags(D23, ["16:40", "18:00", "19:20"], reason="operational"), notices())
    assert (by["18:00"]["label"], by["18:00"]["confidence"]) == ("operated", "at_risk")
    assert (by["19:20"]["label"], by["19:20"]["operated_leg"]) == ("diverted", "Brodick-Troon")


def test_a_departure_dropped_before_it_was_due_did_not_run():
    ns = notices()
    # Pretend the 11:53 Ardrossan-route notice dropped Troon 17:00 before it was due.
    for n in ns:
        if n["route_code"] == "003" and n["updated_at"] >= at(D24, "13:00"):
            n["detail"] = n["detail"].replace("**Depart** Troon – 17:00", "")
    runs = L.diversion_runs(ns, D24)["003"]
    assert ("Troon", "17:00") not in {(d["origin"], d["local"]) for d in runs}
    assert ("Troon", "07:00") in {(d["origin"], d["local"]) for d in runs}  # dropped only after it sailed


def test_a_notice_cancellation_still_wins():
    events = flags(D24, ARD + BRO) + flags(D24, ["12:30"], kind="cancelled", updated=at(D24, "10:00"))
    by = label(D24, sailings(D24), events, notices())
    assert (by["12:30"]["label"], by["12:30"]["confidence"]) == ("cancelled_weather", "notice")
    assert by["09:45"]["label"] == "diverted"  # Troon 11:30 pairs with the next-nearest Ardrossan sailing


def test_unpairable_departure_leaves_the_day_unresolved():
    ns = notices()
    for n in ns:  # a Troon departure with no Ardrossan sailing within reach
        n["detail"] = (n["detail"] or "").replace("**Depart** Troon 07:00", "**Depart** Troon 03:00")
    by = label(D24, sailings(D24, removed=["08:20"]), flags(D24, ARD + BRO), ns)
    r = by["09:45"]
    assert (r["train"], r["exclusion"]) == (False, "diversion_day_unresolved")
    assert "diversion_day_unresolved" in L.NOT_SCORED


def test_no_notices_means_no_change():
    by = label(D24, sailings(D24), flags(D24, ["15:20"]), [])
    assert (by["15:20"]["label"], by["15:20"]["confidence"]) == ("operated", "at_risk")
