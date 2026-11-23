import json
from datetime import datetime, timedelta, timezone

import pytest

from ferrypredictor import forecast, parse, site, storage
from ferrypredictor.collect import NOTICE_SCHEMA, ROUTE_STATUS_SCHEMA, SAILING_SCHEMA
from test_parse import NOTICES, load_sailing_rows

NOW = datetime(2026, 11, 23, 20, 0, tzinfo=timezone.utc)


def fc_rows(model, run, start, hours, point="mid_troon", speed=20.0, gust=30.0):
    rows = []
    for h in range(hours):
        t = start + timedelta(hours=h)
        row = {k: None for k in forecast.FORECAST_SCHEMA.names}
        row.update(model=model, run_time=run, fetched_at=run + timedelta(hours=6), point=point, valid_time=t,
                   lead_hours=h, wind_speed_kn=speed, wind_gust_kn=gust, wind_dir_deg=90.0)
        rows.append(row)
    return rows


@pytest.fixture
def store(tmp_path):
    data = tmp_path / "data"
    storage.append_rows(data / "notices", NOTICES, NOTICE_SCHEMA, NOW)
    storage.append_rows(data / "sailings", load_sailing_rows(), SAILING_SCHEMA, NOW)
    parse.run_parse(NOW, data, tmp_path / "site")
    start = datetime(2026, 11, 23, 20, tzinfo=timezone.utc)
    rows = fc_rows("ukmo_uk_deterministic_2km", datetime(2026, 11, 23, 15, tzinfo=timezone.utc), start, 30, speed=25, gust=36)
    rows += fc_rows("ecmwf_ifs", datetime(2026, 11, 23, 12, tzinfo=timezone.utc), start, 120, speed=12, gust=18)
    rows += fc_rows("ecmwf_ifs", datetime(2026, 11, 23, 6, tzinfo=timezone.utc), start, 120, speed=99, gust=99)  # older run
    for r in fc_rows("marine", NOW, start, 120):
        r.update(run_time=None, fetched_at=NOW, wind_speed_kn=None, wind_gust_kn=None, wind_dir_deg=None, wave_height_m=1.24, wave_period_s=5.0)
        rows.append(r)
    storage.append_rows(data / "forecasts", rows, forecast.FORECAST_SCHEMA, NOW)
    storage.append_rows(data / "route_status", [
        {"route_code": "301", "observed_at": NOW, "status": "DISRUPTIONS", "effective_status": "ALL_SAILINGS_CANCELLED", "vessels": "ALFRED"},
        {"route_code": "003", "observed_at": NOW, "status": "BE_AWARE", "effective_status": "DISRUPTIONS", "vessels": "CALEDONIAN ISLES"},
    ], ROUTE_STATUS_SCHEMA, NOW)
    return tmp_path


def sailings(payload, day, route="301"):
    d = next(d for d in payload["days"] if d["date"] == day)
    return {s["depart"]: s for s in d["sailings"] if s["route"] == route}


def test_five_days_of_sailings_with_calmac_word(store):
    p = site.build(NOW, store / "data", store / "site")
    assert [d["date"] for d in p["days"]] == ["2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27"]
    tue = sailings(p, "2026-11-24")
    assert tue["07:30"]["removed"] and "cancelled" in tue["07:30"]["calmac"]
    assert tue["10:15"]["calmac"][0] == "cancelled"  # cancelled outranks at_risk
    mon = sailings(p, "2026-11-23")
    assert mon["18:10"]["calmac"] == ["cancelled"] and mon["07:30"]["departed"]


def test_met_office_wind_first_then_ecmwf_from_the_latest_run(store):
    p = site.build(NOW, store / "data", store / "site")
    tue = sailings(p, "2026-11-24")
    assert tue["10:15"]["wind"]["model"] == "ukmo_uk_deterministic_2km" and tue["10:15"]["wind"]["gust_kn"] == 36
    fri = sailings(p, "2026-11-27")
    first = fri[min(fri)]
    assert first["wind"]["model"] == "ecmwf_ifs" and first["wind"]["gust_kn"] == 18  # not the older 99s
    assert first["waves"] == {"height_m": 1.2, "period_s": 5.0}


def test_route_status_and_live_notices(store):
    p = site.build(NOW, store / "data", store / "site")
    troon = next(r for r in p["routes"] if r["code"] == "301")
    assert troon["name"] == "Troon to Brodick" and troon["status"] == "ALL_SAILINGS_CANCELLED"
    # The Monday notice ended at 20:00, so only Tuesday's is still live.
    assert [n["title"] for n in troon["notices"]] == ["Tuesday 24 November"]
    assert all("**" not in n["text"] for n in troon["notices"])
    earlier = site.build(NOW - timedelta(minutes=30), store / "data", store / "site")
    troon = next(r for r in earlier["routes"] if r["code"] == "301")
    assert [n["level"] for n in troon["notices"]] == ["ALL_SAILINGS_CANCELLED", "DISRUPTIONS"]  # worst first


def test_superseded_notice_versions_no_longer_count():
    def version(hhmm, detail):
        return {"notice_id": "n1", "route_code": "301", "notice_type": "SAILING", "is_active": True, "end_at": None,
                "updated_at": datetime(2026, 11, 23, int(hhmm[:2]), int(hhmm[3:]), tzinfo=timezone.utc),
                "first_seen_at": NOW, "title": "Tuesday 24 November", "detail": detail, "reason": "weather"}
    v1, v2 = version("10:00", "The 10:15 departing Troon is cancelled."), version("12:00", "Service is liable to disruption at short notice.")
    tt = parse.build_timetable(load_sailing_rows())
    events = parse.parse_notice(v1, tt, NOW)[0] + parse.parse_notice(v2, tt, NOW)[0]
    live = site.active_versions([v1, v2], NOW)
    current = site.current_events(events, live)
    key = next(e["sailing_key"] for e in events if e["event_type"] == "cancelled")
    assert current[key] == {"at_risk"}


def test_run_site_writes_page_and_data(store):
    out = store / "public"
    summary = site.run_site(NOW, store / "data", store / "site", out)
    assert summary["days"] == 5 and summary["sailings"] > 50
    assert (out / "index.html").exists()
    payload = json.loads((out / "sailings.json").read_text())
    assert payload["generated_at"] == "2026-11-23T20:00:00+00:00"
