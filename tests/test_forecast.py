import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from ferrypredictor import forecast, storage

NOW = datetime(2026, 11, 23, 6, 50, tzinfo=timezone.utc)
RUN = datetime(2026, 11, 23, 0, 0, tzinfo=timezone.utc)
POINTS = ["ardrossan", "troon", "brodick", "mid_ardrossan", "mid_troon"]


def hours(start, n):
    return [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]


def wind_payload(n_hours=12, start=datetime(2026, 11, 23, 0, 0)):
    return [
        {"latitude": 55.6, "longitude": -5.0, "hourly": {
            "time": hours(start, n_hours),
            "wind_speed_10m": [10.0 + i for i in range(n_hours)],
            "wind_gusts_10m": [20.0 + i for i in range(n_hours)],
            "wind_direction_10m": [90.0] * n_hours,
        }}
        for _ in POINTS
    ]


def marine_payload(n_hours=12, start=datetime(2026, 11, 23, 0, 0)):
    out = []
    for p in POINTS:
        on_land = p in ("troon", "ardrossan")
        out.append({"latitude": 55.6, "longitude": -5.0, "hourly": {
            "time": hours(start, n_hours),
            "wave_height": [None if on_land else 1.2] * n_hours,
            "wave_period": [None if on_land else 5.0] * n_hours,
            "wave_direction": [None if on_land else 100.0] * n_hours,
        }})
    return out


def meta(init):
    return {"last_run_initialisation_time": int(init.timestamp()), "update_interval_seconds": 21600}


@pytest.fixture
def api():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(forecast.FORECAST_URL, params={"models": "ecmwf_ifs"}).mock(return_value=httpx.Response(200, json=wind_payload()))
        mock.get(forecast.FORECAST_URL, params={"models": "ukmo_uk_deterministic_2km"}).mock(
            return_value=httpx.Response(200, json=wind_payload(n_hours=10))
        )
        mock.get(forecast.MARINE_URL).mock(return_value=httpx.Response(200, json=marine_payload()))
        mock.get(forecast.meta_url("ecmwf_ifs")).mock(return_value=httpx.Response(200, json=meta(RUN)))
        mock.get(forecast.meta_url("ukmo_uk_deterministic_2km")).mock(return_value=httpx.Response(200, json=meta(RUN + timedelta(hours=5))))
        yield mock


def rows(tmp_path):
    return storage.read_dataset(tmp_path / "data" / "forecasts", forecast.FORECAST_SCHEMA).to_pylist()


def run(tmp_path, now=NOW):
    return forecast.run_forecast("test@example.com", now, tmp_path / "data", tmp_path / "site", retry_wait=0)


def test_writes_rows_for_every_model_point_and_future_hour(api, tmp_path):
    summary = run(tmp_path)
    # Hours from 06:00 (the hour containing now) to 11:00 for 12-hour payloads: 6 hours; 4 hours for UKMO.
    assert summary["rows"] == {"ecmwf_ifs": 30, "ukmo_uk_deterministic_2km": 20, "marine": 30}
    r = rows(tmp_path)
    assert {x["point"] for x in r} == set(POINTS)
    ec = [x for x in r if x["model"] == "ecmwf_ifs" and x["point"] == "brodick"]
    assert ec[0]["run_time"] == RUN
    assert ec[0]["valid_time"] == datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc)
    assert ec[0]["lead_hours"] == pytest.approx(6.0)
    assert ec[0]["wind_gust_kn"] == pytest.approx(26.0)


def test_marine_nulls_are_kept(api, tmp_path):
    run(tmp_path)
    troon = [x for x in rows(tmp_path) if x["model"] == "marine" and x["point"] == "troon"]
    assert troon and all(x["wave_height_m"] is None for x in troon)
    assert troon[0]["run_time"] is None
    assert troon[0]["lead_hours"] == pytest.approx((troon[0]["valid_time"] - NOW).total_seconds() / 3600)


def test_same_model_run_is_not_written_twice(api, tmp_path):
    run(tmp_path)
    summary = run(tmp_path, now=NOW + timedelta(minutes=5))
    assert summary["rows"]["ecmwf_ifs"] == 0
    assert summary["rows"]["ukmo_uk_deterministic_2km"] == 0
    assert summary["rows"]["marine"] == 30


def test_missing_metadata_falls_back_to_fetch_time(api, tmp_path):
    api.get(forecast.meta_url("ecmwf_ifs")).mock(return_value=httpx.Response(404))
    run(tmp_path)
    ec = [x for x in rows(tmp_path) if x["model"] == "ecmwf_ifs"]
    assert ec[0]["run_time"] is None
    assert ec[0]["lead_hours"] == pytest.approx(-50 / 60)


def test_failed_request_still_writes_the_rest_then_raises(api, tmp_path):
    api.get(forecast.MARINE_URL).mock(return_value=httpx.Response(429, json={"error": True, "reason": "Daily API request limit exceeded"}))
    with pytest.raises(forecast.ForecastError, match="marine"):
        run(tmp_path)
    models = {x["model"] for x in rows(tmp_path)}
    assert models == {"ecmwf_ifs", "ukmo_uk_deterministic_2km"}
    health = json.loads((tmp_path / "site" / "health" / "forecast.json").read_text())
    assert "marine" in health["error"]
