"""FOI label normaliser, tested on made-up rows shaped like CalMac's FOI 4405 export."""

from datetime import date, datetime, time, timezone

import pyarrow.parquet as pq
import pytest

from ferrypredictor import foi

openpyxl = pytest.importorskip("openpyxl")

HEADER = ["Year", "Month", "Scheduled Departure Date", "Scheduled Departure Time", "Scheduled Arrival Time",
          "Actual Departure Date", "Actual Departure Time", "Actual Arrival Time", "Scheduled Passage Minutes",
          "Actual Passage Minutes", "Route", "Scheduled Leg", "Operated Leg", "Ship Name", "Contractual Outcome",
          "Cancelled (Weather)", "Cancelled (Technical)", "Cancelled (Other)", "Late (Weather)", "Late (Tech)",
          "Late (Other)", "Diverted Sailings", "Returned to Port Sailings"]


def row(d, dep, arr, leg, ship="MV Caledonian Isles", outcome="On Time", act=None, operated=None, **flags):
    act_dep, act_arr = act or (None, None)
    values = {
        "Year": str(d.year), "Month": d.strftime("%B"), "Scheduled Departure Date": datetime.combine(d, time()),
        "Scheduled Departure Time": dep, "Scheduled Arrival Time": arr,
        "Actual Departure Date": datetime.combine(d, time()) if act_dep else None,
        "Actual Departure Time": act_dep, "Actual Arrival Time": act_arr,
        "Scheduled Passage Minutes": 55, "Actual Passage Minutes": 0, "Route": "Ardrossan - Brodick",
        "Scheduled Leg": leg, "Operated Leg": operated or leg, "Ship Name": ship, "Contractual Outcome": outcome,
    }
    for h in HEADER[15:]:
        values[h] = flags.get(h, 0)
    return values


ROWS = [
    row(date(2025, 1, 10), time(7, 0), time(7, 55), "Ardrossan-Brodick", act=(time(7, 4), time(8, 0))),
    row(date(2025, 1, 10), time(8, 20), time(9, 15), "Brodick-Ardrossan", outcome="Cancelled", **{"Cancelled (Weather)": 1}),
    row(date(2025, 7, 15), time(9, 45), time(10, 40), "Ardrossan-Brodick", outcome="Level 1 Lateness",
        act=(time(10, 5), time(11, 0)), **{"Late (Weather)": 1}),
    row(date(2025, 1, 11), time(19, 20), time(20, 15), "Brodick-Ardrossan", operated="Brodick-Troon",
        act=(time(19, 25), time(20, 50)), **{"Diverted Sailings": 1}),
    row(date(2025, 2, 3), time(10, 15), time(11, 35), "Troon-Brodick", ship="MV Alfred", outcome="Cancelled",
        **{"Cancelled (Technical)": 1}),
    row(date(2024, 5, 1), time(12, 30), time(13, 25), "Ardrossan-Brodick", ship="MV Isle of Arran Dummy", outcome="Not Operated"),
    row(date(2024, 5, 1), time(21, 0), time(21, 55), "Ardrossan-Brodick", outcome="Additional", act=(time(21, 0), time(21, 55))),
    row(date(2021, 1, 5), time(15, 20), time(16, 15), "Ardrossan-Brodick", outcome="Cancelled", **{"Cancelled (Other)": 1}),
    row(date(2025, 3, 1), time(9, 0), time(10, 10), "Gourock (Dunoon)-Brodick"),
    # Two-vessel timetable: both ends depart at 07:00; only the Brodick sailing was cancelled.
    row(date(2021, 7, 2), time(7, 0), time(7, 55), "Ardrossan-Brodick", act=(time(7, 0), time(7, 55))),
    row(date(2021, 7, 2), time(7, 0), time(7, 55), "Brodick-Ardrossan", outcome="Cancelled", **{"Cancelled (Other)": 1}),
]


@pytest.fixture
def annexes(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Export"
    ws.append(HEADER)
    for r in ROWS:
        ws.append([r[h] for h in HEADER])
    e = tmp_path / "annex_e.xlsx"
    wb.save(e)
    d = tmp_path / "annex_d.csv"
    d.write_text(
        "﻿Route,Sched Dept Date,Sched Dept Time,Dominant Reason\n"
        "Ardrossan - Brodick,10/01/2025,8:20 AM,ADVERSE WEATHER\n"
        "Ardrossan - Brodick,05/01/2021,3:20 PM,SCOT GOVERNMENT APPROVED\n"
        "Ardrossan - Brodick,02/07/2021,7:00 AM,SCOT GOVERNMENT APPROVED\n"
        "Oban - Craignure,10/01/2025,8:20 AM,TECH ISSUE (THIS SHIP)*\n",
        encoding="utf-8",
    )
    return e, d


def labelled(annexes):
    e, d = annexes
    return {(r["sailing_date"], r["departure_local"], r["origin"]): r for r in foi.normalise(foi.read_annex_e(e), foi.read_annex_d(d))}


def find(rows, d, hhmm):
    (match,) = [r for k, r in rows.items() if k[0] == d and k[1] == hhmm]
    return match


def test_labels_and_keys_match_the_live_log(annexes):
    rows = labelled(annexes)
    first = find(rows, date(2025, 1, 10), "07:00")
    assert first["label"] == "operated" and first["train"]
    assert first["sailing_key"] == "003|Ardrossan|2025-01-10T07:00:00+00:00"
    assert first["vessel"] == "CALEDONIAN ISLES" and first["delay_minutes"] == 4
    assert find(rows, date(2025, 1, 10), "08:20")["label"] == "cancelled_weather"
    assert find(rows, date(2025, 2, 3), "10:15")["label"] == "cancelled_technical"
    assert find(rows, date(2025, 2, 3), "10:15")["route_code"] == "301"


def test_summer_times_convert_to_utc_and_lateness_is_kept(annexes):
    r = find(labelled(annexes), date(2025, 7, 15), "09:45")
    assert r["departure_at"] == datetime(2025, 7, 15, 8, 45, tzinfo=timezone.utc)
    assert r["label"] == "operated" and r["late_reason"] == "weather" and r["delay_minutes"] == 20


def test_diversion_from_flag_or_changed_leg(annexes):
    r = find(labelled(annexes), date(2025, 1, 11), "19:20")
    assert r["label"] == "diverted" and r["operated_leg"] == "Brodick-Troon"
    assert foi.label_for({"Scheduled Leg": "Brodick-Ardrossan", "Operated Leg": "Brodick-Troon"}) == "diverted"


def test_placeholders_extras_covid_cuts_and_other_legs_are_excluded(annexes):
    rows = labelled(annexes)
    assert find(rows, date(2024, 5, 1), "12:30")["exclusion"] == "not_operated_placeholder"
    assert find(rows, date(2024, 5, 1), "21:00")["exclusion"] == "additional_unscheduled"
    covid = find(rows, date(2021, 1, 5), "15:20")
    assert covid["reason_detail"] == "SCOT GOVERNMENT APPROVED" and covid["exclusion"] == "planned_timetable_reduction"
    other = find(rows, date(2025, 3, 1), "09:00")
    assert other["exclusion"] == "not_an_arran_route_leg" and other["sailing_key"] is None
    assert find(rows, date(2025, 1, 10), "08:20")["reason_detail"] == "ADVERSE WEATHER"  # Oban row ignored


def test_cli_writes_parquet(annexes, tmp_path, capsys):
    e, d = annexes
    out = tmp_path / "labels" / "foi4405.parquet"
    assert foi.main([str(e), str(d), str(out)]) == 0
    table = pq.read_table(out)
    assert table.num_rows == len(ROWS) and table.schema.equals(foi.LABEL_SCHEMA)
    assert "'train': 6" in capsys.readouterr().out


def test_reason_codes_attach_only_to_the_cancelled_sailing_at_a_shared_minute(annexes):
    rows = labelled(annexes)
    ran = rows[(date(2021, 7, 2), "07:00", "Ardrossan")]
    cut = rows[(date(2021, 7, 2), "07:00", "Brodick")]
    assert ran["reason_detail"] is None and ran["train"]
    assert cut["reason_detail"] == "SCOT GOVERNMENT APPROVED" and cut["exclusion"] == "planned_timetable_reduction"
