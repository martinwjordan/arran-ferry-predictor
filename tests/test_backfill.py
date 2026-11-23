from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import respx

from ferrypredictor import backfill, storage
from ferrypredictor.forecast import ForecastError

NOW = datetime(2026, 11, 23, 20, 0, tzinfo=timezone.utc)
POINTS = ["ardrossan", "troon", "brodick", "mid_ardrossan", "mid_troon"]


def archive_payload(first: date, last: date, gusts=True, lead_days=range(0, 6)):
    hours = int(((last - first).days + 1) * 24)
    times = [(datetime(first.year, first.month, first.day) + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(hours)]
    out = []
    for i, _ in enumerate(POINTS):
        hourly = {"time": times}
        for n in range(0, 6):
            present = n in lead_days
            hourly[f"wind_speed_10m_previous_day{n}"] = [10.0 + n if present else None] * hours
            hourly[f"wind_gusts_10m_previous_day{n}"] = [20.0 + n if (present and gusts) else None] * hours
            hourly[f"wind_direction_10m_previous_day{n}"] = [200.0 if present else None] * hours
        out.append({"latitude": 55.5 + i / 100, "longitude": -5.0 + i / 100, "hourly": hourly})
    return out


def responder(request):
    q = request.url.params
    first, last = date.fromisoformat(q["start_date"]), date.fromisoformat(q["end_date"])
    return httpx.Response(200, json=archive_payload(first, last))


def run(tmp_path, **kw):
    kw.setdefault("models", ["gfs_seamless"])
    kw.setdefault("start", date(2024, 1, 30))
    kw.setdefault("end", date(2024, 3, 2))
    return backfill.run_backfill(
        "test@example.com", NOW, tmp_path / "data", tmp_path / "site", pause=0, retry_wait=0, minute_wait=0, **kw
    )


def rows(tmp_path):
    return storage.read_dataset(tmp_path / "data" / "forecast_archive", backfill.ARCHIVE_SCHEMA).to_pylist()


def test_months_are_clipped_to_the_range():
    assert backfill.months(date(2024, 1, 30), date(2024, 3, 2)) == [
        (date(2024, 1, 30), date(2024, 1, 31)),
        (date(2024, 2, 1), date(2024, 2, 29)),
        (date(2024, 3, 1), date(2024, 3, 2)),
    ]


def test_request_asks_for_sea_cells_and_every_lead_day(tmp_path):
    names = backfill.hourly_variables().split(",")
    assert "wind_gusts_10m_previous_day5" in names and "wind_speed_10m_previous_day0" in names
    assert len(names) == 18
    with respx.mock() as mock:
        route = mock.get(
            backfill.ARCHIVE_URL,
            params={"cell_selection": "sea", "models": "gfs_seamless", "wind_speed_unit": "kn", "hourly": ",".join(names)},
        ).mock(side_effect=responder)
        run(tmp_path, start=date(2024, 1, 31), end=date(2024, 1, 31))
    assert route.call_count == 1


def test_rows_per_point_hour_and_lead_day_with_grid_cell(tmp_path):
    with respx.mock() as mock:
        mock.get(backfill.ARCHIVE_URL, params={"cell_selection": "sea"}).mock(side_effect=responder)
        summary = run(tmp_path, start=date(2024, 1, 31), end=date(2024, 1, 31))
    assert summary == {"chunks_fetched": 1, "rows": 5 * 24 * 6, "chunks_done": 1, "chunks_total": 1}
    r = [x for x in rows(tmp_path) if x["point"] == "brodick" and x["lead_day"] == 3]
    assert len(r) == 24
    assert r[0]["wind_speed_kn"] == 13.0 and r[0]["wind_gust_kn"] == 23.0
    assert r[0]["valid_time"] == datetime(2024, 1, 31, 0, 0, tzinfo=timezone.utc)
    assert (r[0]["grid_lat"], r[0]["grid_lon"]) == pytest.approx((55.52, -4.98))


def test_lead_days_outside_the_archive_are_dropped(tmp_path):
    payload = archive_payload(date(2024, 1, 31), date(2024, 1, 31), lead_days=range(0, 2))
    with respx.mock() as mock:
        mock.get(backfill.ARCHIVE_URL).mock(return_value=httpx.Response(200, json=payload))
        run(tmp_path, start=date(2024, 1, 31), end=date(2024, 1, 31))
    assert {x["lead_day"] for x in rows(tmp_path)} == {0, 1}


def test_runs_resume_where_the_last_one_stopped(tmp_path):
    with respx.mock() as mock:
        route = mock.get(backfill.ARCHIVE_URL).mock(side_effect=responder)
        first = run(tmp_path, max_chunks=2)
        second = run(tmp_path, max_chunks=2)
        third = run(tmp_path, max_chunks=2)
    assert (first["chunks_fetched"], second["chunks_fetched"], third["chunks_fetched"]) == (2, 1, 0)
    assert third["chunks_done"] == third["chunks_total"] == 3
    assert route.call_count == 3
    months_seen = sorted({x["valid_time"].strftime("%Y-%m") for x in rows(tmp_path)})
    assert months_seen == ["2024-01", "2024-02", "2024-03"]


def test_daily_limit_stops_cleanly_and_keeps_finished_chunks(tmp_path):
    calls = {"n": 0}

    def limited(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return responder(request)
        return httpx.Response(429, json={"error": True, "reason": "Daily API request limit exceeded. Please try again tomorrow."})

    with respx.mock() as mock:
        mock.get(backfill.ARCHIVE_URL).mock(side_effect=limited)
        summary = run(tmp_path)
    assert summary["chunks_fetched"] == 1
    assert "Daily API request limit" in summary["stopped"]
    done = storage.load_state(tmp_path / "data" / "state" / "backfill_done.json", {})
    assert list(done) == ["gfs_seamless|2024-01"]


def test_minute_limit_is_waited_out_once(tmp_path):
    calls = {"n": 0}

    def busy(request):
        calls["n"] += 1
        if calls["n"] <= 2:  # both attempts of the first fetch
            return httpx.Response(429, json={"error": True, "reason": "Minutely API request limit exceeded."})
        return responder(request)

    with respx.mock() as mock:
        mock.get(backfill.ARCHIVE_URL).mock(side_effect=busy)
        summary = run(tmp_path, start=date(2024, 1, 31), end=date(2024, 1, 31))
    assert summary["chunks_fetched"] == 1 and "stopped" not in summary


def test_other_errors_fail_the_run_and_are_recorded(tmp_path):
    with respx.mock() as mock:
        mock.get(backfill.ARCHIVE_URL).mock(return_value=httpx.Response(400, json={"error": True, "reason": "Bad model"}))
        with pytest.raises(ForecastError, match="gfs_seamless\\|2024-01: HTTP 400 Bad model"):
            run(tmp_path)
    health = storage.load_state(tmp_path / "site" / "health" / "backfill.json", {})
    assert "HTTP 400" in health["error"]
