import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from ferrypredictor import parse, storage
from ferrypredictor.collect import NOTICE_SCHEMA, SAILING_SCHEMA

FIX = Path(__file__).parent / "fixtures" / "calmac"
NOW = datetime(2026, 11, 23, 20, 0, tzinfo=timezone.utc)
D23, D24 = date(2026, 11, 23), date(2026, 11, 24)


def _dt(v):
    return datetime.fromisoformat(v) if v else None


def load_notices():
    out = []
    for n in json.loads((FIX / "notices_2026-11-23.json").read_text()):
        for k in ("updated_at", "first_seen_at", "start_at", "end_at", "next_review_at"):
            n[k] = _dt(n.get(k))
        out.append(n)
    return out


def load_sailing_rows():
    out = []
    for r in json.loads((FIX / "sailing_rows_2026-11-23.json").read_text()):
        for k in ("observed_at", "departure_at", "arrival_at"):
            r[k] = _dt(r[k])
        r["sailing_date"] = date.fromisoformat(r["sailing_date"])
        out.append(r)
    return out


NOTICES = load_notices()
TIMETABLE = parse.build_timetable(load_sailing_rows())


def notice(id_suffix, updated_hhmm):
    matches = [n for n in NOTICES if n["notice_id"].endswith(id_suffix) and f"{n['updated_at']:%H:%M}" == updated_hhmm]
    assert len(matches) == 1, (id_suffix, updated_hhmm)
    return matches[0]


def events(n, kind=None):
    ev, _ = parse.parse_notice(n, TIMETABLE, NOW)
    return {(e["sailing_date"], e["origin"], e["departure_local"]) for e in ev if kind is None or e["event_type"] == kind}


def unparsed(n):
    return parse.parse_notice(n, TIMETABLE, NOW)[1]


# --- the spec's required cases (design spec, section 10) -------------------------------------


def test_troon_notice_cancels_the_1810_and_the_next_morning_0730():
    n = notice("1c971924", "12:18")  # "Monday 23 - Tuesday 24 November"
    assert events(n, "cancelled") == {(D23, "Troon", "18:10"), (D24, "Brodick", "07:30")}
    assert unparsed(n) == []


def test_ardrossan_notice_warns_of_diversion_and_cancels_nothing():
    n = notice("05d97b5c", "09:55")
    assert events(n, "at_risk_diversion") == {
        (D23, "Brodick", "13:55"), (D23, "Ardrossan", "15:20"), (D23, "Brodick", "16:40"),
        (D23, "Ardrossan", "18:00"), (D23, "Brodick", "19:20"),
        (D24, "Ardrossan", "07:00"),
    }
    assert events(n, "cancelled") == set()
    ev, _ = parse.parse_notice(n, TIMETABLE, NOW)
    assert [(e["sailing_date"], e["review_time"]) for e in ev if e["event_type"] == "review_at"] == [(D23, "13:15")]
    assert all(e["diversion_port"] == "Troon" for e in ev if e["event_type"] == "at_risk_diversion")


# --- other real wording -----------------------------------------------------------------------


def test_whole_day_warning_covers_every_sailing_that_day():
    n = notice("05d97b5c", "09:55")  # "service is liable to disruption or cancellation at short notice"
    day = {k for k in events(n, "at_risk") if k[0] == D23}
    assert len(day) == 10


def test_cancelled_list_under_a_heading():
    n = notice("69a86143", "17:38")  # "the following services are cancelled:" then Depart lines
    assert {(D24, "Troon", "10:15"), (D24, "Brodick", "12:05")} <= events(n, "cancelled")


def test_diversion_timetable_matches_exact_times_and_flags_the_rest():
    n = notice("4b2a3582", "15:32")  # Caledonian Isles via Troon: Depart Troon 07:00, Brodick 09:10, Troon 11:30
    assert events(n, "diverted") == {(D24, "Ardrossan", "07:00")}
    flagged = sorted(u["sentence"] for u in unparsed(n))
    assert flagged == ["Depart Brodick 09:10", "Depart Troon 11:30"]


def test_nearby_diversion_departure_matches_with_lower_confidence():
    n = notice("05d97b5c", "13:17")  # "Depart Brodick 19:00 ... diverting to Troon"; timetable says 19:20
    ev, _ = parse.parse_notice(n, TIMETABLE, NOW)
    div = [e for e in ev if e["event_type"] == "diverted"]
    assert [(e["origin"], e["departure_local"], e["match"], e["confidence"]) for e in div] == [("Brodick", "19:20", "nearest", 0.8)]


def test_heightened_risk_from_a_sailing_applies_to_it_and_later_ones():
    n = notice("05d97b5c", "13:17")  # "... heightened risk of disruption or cancellation from the 16:40 departing Brodick"
    assert events(n, "at_risk") == {(D23, "Brodick", "16:40"), (D23, "Ardrossan", "18:00"), (D23, "Brodick", "19:20")}


def test_bus_sentences_are_ignored():
    n = notice("05d97b5c", "16:05")  # "A bus will operate from Troon to Ardrossan ... at 20:35"
    ev, un = parse.parse_notice(n, TIMETABLE, NOW)
    assert not any("bus" in e["sentence"].lower() for e in ev + un)


def test_cancelled_sailings_are_kept_in_the_timetable_as_removed():
    t = {(s.date, s.origin, s.local): s for s in TIMETABLE}
    assert t[(D23, "Troon", "18:10")].removed
    assert t[(D24, "Brodick", "07:30")].removed
    assert not t[(D23, "Ardrossan", "07:00")].removed  # reissued id at 16:02, still listed


# --- synthetic edge cases ---------------------------------------------------------------------


def synthetic(detail, title="Tuesday 24 November", route="301"):
    return {
        "notice_id": "routestatus-test", "route_code": route, "updated_at": datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc),
        "title": title, "detail": detail, "reason": "weather", "notice_type": "SAILING",
    }


def test_a_named_sailing_with_no_rule_is_flagged_not_guessed():
    n = synthetic("The 10:15 departing Troon will be operated by MV Alfred.")
    ev, un = parse.parse_notice(n, TIMETABLE, NOW)
    assert ev == [] and [u["why"] for u in un] == ["names a sailing but matches no rule"]


def test_reinstated_and_will_not_operate():
    n = synthetic("The 10:15 departing Troon is reinstated.\r\n\r\nThe 12:05 departing Brodick will not operate.")
    assert events(n, "reinstated") == {(D24, "Troon", "10:15")}
    assert events(n, "cancelled") == {(D24, "Brodick", "12:05")}


def test_unknown_sailing_is_flagged():
    n = synthetic("The 09:00 departing Troon is cancelled.")
    assert [u["why"] for u in unparsed(n)] == ["no such sailing in the timetable: 09:00 from Troon on 2026-11-24"]


@pytest.mark.parametrize(
    "title,ref,expected",
    [
        ("Thursday 31 December", date(2026, 12, 30), date(2026, 12, 31)),
        ("Friday 1 January", date(2026, 12, 31), date(2027, 1, 1)),
        ("Monday 23 - Tuesday 24 November", date(2026, 11, 23), date(2026, 11, 23)),
        ("Information", date(2026, 11, 23), None),
    ],
)
def test_title_dates_pick_the_right_year(title, ref, expected):
    assert parse.title_date(title, ref) == expected


# --- the job ----------------------------------------------------------------------------------


def seed(tmp_path):
    data = tmp_path / "data"
    storage.append_rows(data / "notices", NOTICES, NOTICE_SCHEMA, NOW)
    storage.append_rows(data / "sailings", load_sailing_rows(), SAILING_SCHEMA, NOW)
    return data


def test_job_parses_each_notice_version_once(tmp_path):
    data = seed(tmp_path)
    first = parse.run_parse(NOW, data, tmp_path / "site")
    assert first["versions_parsed"] == 10 and first["reparsed_all"]
    assert first["events"]["cancelled"] >= 4
    second = parse.run_parse(NOW, data, tmp_path / "site")
    assert second["versions_parsed"] == 0
    got = storage.read_dataset(data / "events", parse.EVENT_SCHEMA).to_pylist()
    assert {(e["sailing_date"], e["origin"], e["departure_local"]) for e in got if e["event_type"] == "cancelled"} >= {
        (D23, "Troon", "18:10"), (D24, "Brodick", "07:30"),
    }
    health = storage.load_state(tmp_path / "site" / "health" / "parse.json", {})
    assert health["details"]["parser_version"] == parse.PARSER_VERSION


def test_new_parser_version_reparses_everything(tmp_path, monkeypatch):
    data = seed(tmp_path)
    parse.run_parse(NOW, data, tmp_path / "site")
    monkeypatch.setattr(parse, "PARSER_VERSION", parse.PARSER_VERSION + 1)
    again = parse.run_parse(NOW, data, tmp_path / "site")
    assert again["reparsed_all"] and again["versions_parsed"] == 10
