"""The predict job on the real 23 November 2026 notices and timetable, with made-up forecasts."""

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from ferrypredictor import features as F
from ferrypredictor import forecast, parse, predict, storage
from ferrypredictor import train as T
from ferrypredictor.collect import NOTICE_SCHEMA, ROUTE_STATUS_SCHEMA, SAILING_SCHEMA
from test_bundle import all_leads, synthetic_labels
from test_model import synthetic
from test_parse import NOTICES, load_sailing_rows

NOW = datetime(2026, 11, 23, 20, 0, tzinfo=timezone.utc)
RUN = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
POINTS = ["ardrossan", "troon", "brodick", "mid_ardrossan", "mid_troon"]


def forecast_rows(run=RUN, gale_day=datetime(2026, 11, 26, tzinfo=timezone.utc), step=1):
    rows = []
    for model in (F.GUST_MODEL, F.SECOND_MODEL, "ecmwf_ifs"):
        for p in POINTS:
            for h in range(0, 24 * 6, step):
                t = run + timedelta(hours=h)
                gale = gale_day <= t < gale_day + timedelta(days=1)
                row = {k: None for k in forecast.FORECAST_SCHEMA.names}
                row.update(model=model, run_time=run, fetched_at=run + timedelta(hours=7), point=p, valid_time=t,
                           lead_hours=h, wind_speed_kn=40.0 if gale else 10.0, wind_gust_kn=60.0 if gale else 15.0,
                           wind_dir_deg=240.0)
                rows.append(row)
    return rows


@pytest.fixture(scope="module")
def model_path(tmp_path_factory):
    df = all_leads(synthetic(n_days=400))
    path = tmp_path_factory.mktemp("model") / "weather.json"
    T.fit(df, synthetic_labels(df), pd.Timestamp("2025-02-01")).save(path)
    return path


@pytest.fixture
def store(tmp_path):
    data = tmp_path / "data"
    storage.append_rows(data / "notices", NOTICES, NOTICE_SCHEMA, NOW)
    storage.append_rows(data / "sailings", load_sailing_rows(), SAILING_SCHEMA, NOW)
    storage.append_rows(data / "route_status", [
        {"route_code": "003", "observed_at": NOW, "status": "BE_AWARE", "effective_status": "BE_AWARE", "vessels": "MV Caledonian Isles"},
        {"route_code": "301", "observed_at": NOW, "status": "DISRUPTIONS", "effective_status": "DISRUPTIONS", "vessels": "MV Alfred, MV Glen Sannox"},
    ], ROUTE_STATUS_SCHEMA, NOW)
    storage.append_rows(data / "forecasts", forecast_rows(), forecast.FORECAST_SCHEMA, NOW)
    parse.run_parse(NOW, data, tmp_path / "site")
    return tmp_path


def run(store, model_path, now=NOW):
    summary = predict.run_predict(now, store / "data", store / "site", model_path)
    payload = json.loads((store / "site" / "predictions.json").read_text())
    return summary, {(s["date"], s["route"], s["depart"]): s for s in payload["sailings"]}, payload


def test_chances_for_the_next_five_days_with_calmac_first(store, model_path):
    summary, by, payload = run(store, model_path)
    assert payload["model_as_of"] == "2025-02-01" and payload["runs"][F.GUST_MODEL] == RUN.isoformat()
    cancelled = by[("2026-11-24", "301", "07:30")]  # cancelled in a notice and dropped from the timetable
    assert cancelled["calmac"] == "cancelled" and cancelled["p_runs"] == 0.0
    ran = by[("2026-11-25", "003", "09:45")]
    assert ran["calmac"] is None and 0.5 < ran["p_runs"] < 1 and ran["lead_day"] == 1
    assert ran["vessel"] == "CALEDONIAN ISLES" and by[("2026-11-25", "301", "10:15")]["vessel"] == "ALFRED"
    assert summary["with_chance"] >= 40


def test_a_forecast_gale_lowers_the_chance(store, model_path):
    _, by, _ = run(store, model_path)
    calm = by[("2026-11-25", "003", "12:30")]["p_runs"]
    gale = by[("2026-11-26", "003", "12:30")]["p_runs"]
    assert gale < calm - 0.3


def test_percentages_for_three_days_then_bands(store, model_path):
    _, by, _ = run(store, model_path)
    assert by[("2026-11-25", "003", "07:00")]["show"] == "percent"
    later = by[("2026-11-26", "003", "07:00")]
    assert later["show"] == "band" and later["band"] in ("Likely", "Uncertain", "Unlikely")
    assert predict.band(0.8) == "Likely" and predict.band(0.5) == "Uncertain" and predict.band(0.2) == "Unlikely"
    assert predict.band(0.7475) == "Likely" and predict.band(0.394) == "Unlikely"  # as the rounded percentage reads


def test_predictions_are_archived_once_per_forecast_run(store, model_path):
    first, _, _ = run(store, model_path)
    second, _, _ = run(store, model_path, now=NOW + timedelta(minutes=20))
    assert first["archived"] > 0 and second["archived"] == 0
    rows = storage.read_dataset(store / "data" / "predictions", predict.PREDICTION_SCHEMA).to_pylist()
    assert len(rows) == first["archived"] and all(r["lead_day"] in (1, 2, 3, 4, 5) for r in rows)


def test_three_hourly_forecasts_still_cover_every_crossing(tmp_path, model_path):
    rows = pd.DataFrame(forecast_rows(step=3))
    f, runs = predict.latest_runs(rows)
    g = f[(f.model == F.GUST_MODEL) & (f.point == "mid_ardrossan")].sort_values("valid_time")
    assert (g.valid_time.diff().dropna() == pd.Timedelta(hours=1)).all() and g.wind_gust_kn.notna().all()
    assert runs[F.GUST_MODEL] == RUN


def test_vessel_override_and_missing_model(store, tmp_path):
    v = predict.operating_vessels([{"route_code": "301", "observed_at": NOW, "vessels": "MV Glen Sannox, MV Alfred"}], {"301": "MV Alfred"})
    assert v == {"301": "ALFRED"}
    summary = predict.run_predict(NOW, store / "data", store / "site", tmp_path / "nope.json")
    assert summary == {"skipped": "no model"}


def test_next_sailings_each_way_for_home_assistant(store, model_path):
    _, by, payload = run(store, model_path)
    nxt = {(d["route"], d["from"]): d["sailings"] for d in payload["next"]}
    assert set(nxt) == {("003", "Ardrossan"), ("003", "Brodick"), ("301", "Troon"), ("301", "Brodick")}
    first = nxt[("003", "Ardrossan")][0]
    assert first["date"] == "2026-11-24" and len(nxt[("003", "Ardrossan")]) == predict.NEXT_PER_DIRECTION
    assert first["chance"] == round(by[("2026-11-24", "003", first["time"])]["p_runs"] * 100)
    cancelled = nxt[("301", "Brodick")][0]  # the 07:30, cancelled and dropped from the timetable
    assert cancelled["calmac"] == "cancelled" and cancelled["chance"] == 0
    assert len(json.dumps(payload["next"])) < 8000  # well inside Home Assistant's attribute limit
