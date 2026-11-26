"""Livestock and timber lights, built on CalMac's real notices of 25 November 2026 for route 301 (Troon to Brodick).

17:32 version: "afternoon sailings are liable to disruption ... no livestock or timber will be carried on these
crossings" (Thursday 26). 17:55 version: "service from the afternoon up to and including the first sailing on
Friday ... No livestock or timber will be carried on these crossings. Following the first crossing on Friday,
livestock and timber will be carried on a case-by-case basis."
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path

from ferrypredictor import cargo
from ferrypredictor.parse import Sailing

UTC = timezone.utc
FIX = Path(__file__).parent / "fixtures" / "calmac"
THU, FRI = date(2026, 11, 26), date(2026, 11, 27)
TIMES = [("Brodick", "07:30"), ("Troon", "10:15"), ("Brodick", "12:05"), ("Troon", "14:10"), ("Brodick", "16:00"), ("Troon", "18:10")]


def notices():
    out = []
    for n in json.loads((FIX / "notices_livestock_2026-11-25.json").read_text()):
        for k in ("updated_at", "first_seen_at", "start_at", "end_at", "next_review_at"):
            n[k] = datetime.fromisoformat(n[k]) if n.get(k) else None
        out.append(n)
    return sorted(out, key=lambda n: n["updated_at"])


def timetable():
    out = []
    for day in (THU, FRI):
        for origin, hhmm in TIMES:
            dest = "Troon" if origin == "Brodick" else "Brodick"
            out.append(Sailing(f"301|{origin}|{day}T{hhmm}", "301", day, origin, dest, hhmm, int(hhmm[:2]) * 60 + int(hhmm[3:])))
    # The Ardrossan route on the same days, which these notices don't cover
    out.append(Sailing(f"003|Ardrossan|{THU}T15:20", "003", THU, "Ardrossan", "Brodick", "15:20", 920))
    return out


def by_slot(found):
    return {(k.split("|")[2][:10], k.split("|")[2][11:]): v["status"] for k, v in found.items()}


def test_classify_sentences():
    assert cargo.classify("Please be advised no livestock or timber will be carried on these crossings.") == "barred"
    assert cargo.classify("Following the first crossing on Friday, livestock and timber will be carried on a case-by-case basis.") == "case_by_case"
    assert cargo.classify("Livestock and timber will now be carried as normal.") == "carried"
    assert cargo.classify("Livestock will not be carried.") == "barred"
    assert cargo.classify("Service is liable to disruption or cancellation at short notice.") is None
    assert cargo.classify("Car parking at Troon is limited.") is None


def test_first_version_bars_thursday_afternoon_only():
    first = notices()[0]
    got = by_slot(cargo.notice_restrictions(first, timetable()))
    assert got == {("2026-11-26", "12:05"): "barred", ("2026-11-26", "14:10"): "barred",
                   ("2026-11-26", "16:00"): "barred", ("2026-11-26", "18:10"): "barred"}


def test_second_version_runs_to_friday_first_sailing_then_case_by_case():
    second = notices()[1]
    got = by_slot(cargo.notice_restrictions(second, timetable()))
    assert got[("2026-11-26", "12:05")] == "barred"
    assert got[("2026-11-26", "18:10")] == "barred"
    assert got[("2026-11-27", "07:30")] == "barred"  # "up to and including the first sailing on Friday"
    for hhmm in ("10:15", "12:05", "14:10", "16:00", "18:10"):
        assert got[("2026-11-27", hhmm)] == "case_by_case"
    assert ("2026-11-26", "07:30") not in got and ("2026-11-26", "10:15") not in got  # Thursday morning: not said
    assert not any(k.startswith("003") for k in cargo.notice_restrictions(second, timetable()))


def test_newest_notice_wins():
    live = {f"v{i}": n for i, n in enumerate(notices())}
    got = by_slot(cargo.restrictions(live, timetable()))
    assert got[("2026-11-27", "07:30")] == "barred"
    assert got[("2026-11-27", "10:15")] == "case_by_case"


def test_lights():
    barred = {"status": "barred", "sentence": "No livestock or timber will be carried on these crossings."}
    case = {"status": "case_by_case", "sentence": "... case-by-case basis."}
    assert cargo.light(barred, set(), False, 0.95, 0.03) == {"light": "red", "source": "calmac", "why": barred["sentence"]}
    assert cargo.light(case, set(), False, 0.95, 0.03)["light"] == "amber"
    assert cargo.light(None, set(), True, None, None)["light"] == "red"  # cancelled
    assert cargo.light(None, set(), False, None, None) is None  # no chance and no CalMac word: say nothing
    assert cargo.light(None, set(), False, 0.35, 0.6) == {"light": "red", "source": "estimate",
                                                          "why": "The sailing itself is unlikely to run."}
    assert cargo.light(None, set(), False, 0.70, 0.2)["light"] == "amber"
    assert cargo.light(None, set(), False, 0.86, 0.12)["light"] == "amber"  # windy enough to restrict
    assert cargo.light(None, {"at_risk"}, False, 0.95, 0.02)["light"] == "amber"
    assert cargo.light(None, set(), False, 0.95, 0.02) == {"light": "green", "source": "estimate",
                                                           "why": "No sign of restrictions in the forecast."}
    assert cargo.light(None, set(), False, 0.748, 0.05)["light"] == "green"  # judged on the whole % shown: 75%


def test_friday_notice_bars_only_the_first_crossing():
    """26 Nov 19:41: "service from the first sailing on Friday is liable to disruption ... No livestock or timber
    will be carried on this crossing. Following the first crossing on Friday, ... case-by-case basis."
    CalMac dated it 26 to 28 November, so Thursday must stay out of it too."""
    n = json.loads((FIX / "notices_livestock_2026-11-26.json").read_text())
    n = next(x for x in n if x["title"] == "Friday 27 November")
    for k in ("updated_at", "first_seen_at", "start_at", "end_at", "next_review_at"):
        n[k] = datetime.fromisoformat(n[k]) if n.get(k) else None
    got = by_slot(cargo.notice_restrictions(n, timetable()))
    assert got == {("2026-11-27", "07:30"): "barred", ("2026-11-27", "10:15"): "case_by_case",
                   ("2026-11-27", "12:05"): "case_by_case", ("2026-11-27", "14:10"): "case_by_case",
                   ("2026-11-27", "16:00"): "case_by_case", ("2026-11-27", "18:10"): "case_by_case"}
