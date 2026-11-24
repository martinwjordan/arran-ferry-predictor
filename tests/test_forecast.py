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


def cell(i):
    """A distinct grid cell per point, as Open-Meteo returns for well-chosen points."""
    return {"latitude": 55.5 + i / 100, "longitude": -5.0 + i / 100}


def wind_payload(n_hours=12, start=datetime(2026, 11, 23, 0, 0)):
    return [
        {**cell(i), "hourly": {
            "time": hours(start, n_hours),
            "wind_speed_10m": [10.0 + i for i in range(n_hours)],
            "wind_gusts_10m": [20.0 + i for i in range(n_hours)],
            "wind_direction_10m": [90.0] * n_hours,
        }}
        for i, _ in enumerate(POINTS)
    ]


def marine_payload(n_hours=12, start=datetime(2026, 11, 23, 0, 0)):
    out = []
    for i, p in enumerate(POINTS):
        on_land = p in ("troon", "ardrossan")
        out.append({**cell(i), "hourly": {
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
        # Routes only match requests that ask for sea grid cells.
        mock.get(forecast.FORECAST_URL, params={"models": "ecmwf_ifs", "cell_selection": "sea"}).mock(
            return_value=httpx.Response(200, json=wind_payload())
        )
        mock.get(forecast.FORECAST_URL, params={"models": "ukmo_uk_deterministic_2km", "cell_selection": "sea"}).mock(
            return_value=httpx.Response(200, json=wind_payload(n_hours=10))
        )
        for model in ("gfs_seamless", "ecmwf_ifs025"):
            mock.get(forecast.FORECAST_URL, params={"models": model, "cell_selection": "sea"}).mock(
                return_value=httpx.Response(200, json=wind_payload(n_hours=8))
            )
        mock.get(forecast.MARINE_URL).mock(return_value=httpx.Response(200, json=marine_payload()))
        mock.get(forecast.meta_url("ecmwf_ifs")).mock(return_value=httpx.Response(200, json=meta(RUN)))
        mock.get(forecast.meta_url("ukmo_uk_deterministic_2km")).mock(return_value=httpx.Response(200, json=meta(RUN + timedelta(hours=5))))
        mock.get(forecast.meta_url("gfs_seamless")).mock(return_value=httpx.Response(200, json=meta(RUN - timedelta(hours=6))))
        mock.get(forecast.meta_url("ecmwf_ifs025")).mock(return_value=httpx.Response(200, json=meta(RUN)))
        yield mock


def rows(tmp_path):
    return storage.read_dataset(tmp_path / "data" / "forecasts", forecast.FORECAST_SCHEMA).to_pylist()


def run(tmp_path, now=NOW):
    return forecast.run_forecast("test@example.com", now, tmp_path / "data", tmp_path / "site", retry_wait=0)


def test_writes_rows_for_every_model_point_and_future_hour(api, tmp_path):
    summary = run(tmp_path)
    # Hours from 06:00 (the hour containing now) to 11:00 for 12-hour payloads: 6 hours; 4 hours for UKMO.
    assert summary["rows"] == {"ecmwf_ifs": 30, "ukmo_uk_deterministic_2km": 20, "gfs_seamless": 10,
                               "ecmwf_ifs025": 10, "marine": 30}
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
    assert summary["rows"]["gfs_seamless"] == 0 and summary["rows"]["ecmwf_ifs025"] == 0
    assert summary["rows"]["marine"] == 30


def test_missing_metadata_falls_back_to_fetch_time(api, tmp_path):
    api.get(forecast.meta_url("ecmwf_ifs")).mock(return_value=httpx.Response(404))
    run(tmp_path)
    ec = [x for x in rows(tmp_path) if x["model"] == "ecmwf_ifs"]
    assert ec[0]["run_time"] is None
    assert ec[0]["lead_hours"] == pytest.approx(-50 / 60)


def test_network_blip_is_retried(api, tmp_path):
    api.get(forecast.MARINE_URL).mock(
        side_effect=[httpx.ConnectTimeout("handshake timed out"), httpx.ConnectTimeout("again"), httpx.Response(200, json=marine_payload())]
    )
    assert run(tmp_path)["rows"]["marine"] == 30


def test_failed_request_still_writes_the_rest_then_raises(api, tmp_path):
    api.get(forecast.MARINE_URL).mock(return_value=httpx.Response(429, json={"error": True, "reason": "Daily API request limit exceeded"}))
    with pytest.raises(forecast.ForecastError, match="marine"):
        run(tmp_path)
    models = {x["model"] for x in rows(tmp_path)}
    assert models == {"ecmwf_ifs", "ukmo_uk_deterministic_2km", "gfs_seamless", "ecmwf_ifs025"}
    health = json.loads((tmp_path / "site" / "health" / "forecast.json").read_text())
    assert "marine" in health["error"]


def test_rows_record_their_grid_cell(api, tmp_path):
    summary = run(tmp_path)
    brodick = [x for x in rows(tmp_path) if x["model"] == "ecmwf_ifs" and x["point"] == "brodick"]
    assert brodick[0]["grid_lat"] == pytest.approx(55.52)
    assert brodick[0]["grid_lon"] == pytest.approx(-4.98)
    assert "shared_grid_cells" not in summary


def test_points_sharing_a_grid_cell_are_flagged(api, tmp_path):
    payload = wind_payload()
    for i in (0, 3, 4):  # ardrossan, mid_ardrossan, mid_troon on one cell, as found live
        payload[i].update(latitude=55.641, longitude=-4.746)
    api.get(forecast.FORECAST_URL, params={"models": "ecmwf_ifs", "cell_selection": "sea"}).mock(
        return_value=httpx.Response(200, json=payload)
    )
    summary = run(tmp_path)
    assert summary["shared_grid_cells"] == {"ecmwf_ifs": [["ardrossan", "mid_ardrossan", "mid_troon"]]}


def test_gfs_run_time_comes_from_its_underlying_model():
    assert forecast.meta_url("gfs_seamless").endswith("/data/ncep_gfs013/static/meta.json")
    assert forecast.meta_url("ecmwf_ifs025").endswith("/data/ecmwf_ifs025/static/meta.json")
