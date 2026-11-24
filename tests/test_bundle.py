"""The JSON model bundle reproduces the fitted models exactly, without scikit-learn at prediction time."""

import json

import numpy as np
import pandas as pd
import pytest

from ferrypredictor import backtest as B
from ferrypredictor import model as M
from ferrypredictor import train as T
from ferrypredictor.bundle import BUNDLE_VERSION, Bundle
from test_model import synthetic


def synthetic_labels(df):
    """Labels table for the non-weather rate: one row per synthetic sailing, a few technical cancellations."""
    rng = np.random.default_rng(7)
    one = df[df.lead_day == 1].drop_duplicates("sailing_key")
    lab = np.where(rng.random(len(one)) < 0.05, "cancelled_technical", np.where(one["y"] == 1, "cancelled_weather", "operated"))
    return pd.DataFrame({"train": True, "route_code": np.where(one["route_troon"] == 1, "301", "003"),
                         "sailing_date": one["sailing_date"].dt.date, "label": lab,
                         "vessel": np.where(one["route_troon"] == 1, "ALFRED", "ISLE OF ARRAN")})


def all_leads(df):
    """The synthetic sailings at every lead day (the real data has one row per sailing and lead day)."""
    return pd.concat([df.assign(lead_day=k) for k in (1, 2, 3, 4, 5)], ignore_index=True)


@pytest.fixture(scope="module")
def fitted():
    df = all_leads(synthetic(n_days=500))
    labels = synthetic_labels(df)
    as_of = pd.Timestamp("2025-04-01")
    return df, labels, as_of, T.fit(df, labels, as_of)


def test_bundle_matches_the_fitted_models(fitted):
    df, _, as_of, bundle = fitted
    train = df[(df.sailing_date < as_of) & (df.lead_day == 1)]
    test = df[(df.sailing_date >= as_of) & (df.lead_day == 1)]
    np.testing.assert_allclose(bundle.logistic(test, 1), M.Logistic().fit(train, train.y).predict(test), atol=1e-5)
    np.testing.assert_allclose(bundle.wind_rule(test, 1), M.WindRule().fit(train, train.y).predict(test), atol=1e-6)
    oot = B.walk_forward(train, B.HISTORY_START, "2025-03-31")
    s = M.Stacker().fit(oot.logistic, oot.wind_rule, oot.y)
    expected = s.predict(bundle.logistic(test, 1), bundle.wind_rule(test, 1))
    np.testing.assert_allclose(bundle.weather(test, 1), expected, atol=1e-3)


def test_round_trip_and_version_guard(fitted, tmp_path):
    _, _, _, bundle = fitted
    path = tmp_path / "model" / "weather.json"
    bundle.save(path)
    again = Bundle.load(path)
    assert again.data == json.loads(path.read_text())
    assert again.data["as_of"] == "2025-04-01" and again.data["data_through"] < "2025-04-01"
    bad = dict(bundle.data, version=BUNDLE_VERSION + 1)
    with pytest.raises(ValueError, match="version"):
        Bundle(bad)


def test_predict_combines_weather_and_non_weather(fitted):
    df, _, as_of, bundle = fitted
    X = df[df.sailing_date >= as_of].copy()
    X["route_code"] = np.where(X.route_troon == 1, "301", "003")
    X["vessel"] = np.where(X.route_troon == 1, "ALFRED", "ISLE OF ARRAN")
    p = bundle.predict(X)
    assert set(p.lead_day) == {1, 2, 3, 4, 5} and p.p_weather.notna().all()
    np.testing.assert_allclose(p.p_runs, (1 - p.p_weather) * (1 - p.p_non_weather))
    assert bundle.non_weather("003", "ISLE OF ARRAN") == bundle.data["non_weather"]["by_vessel"]["003|ISLE OF ARRAN"]
    assert bundle.non_weather("003", "NEW SHIP") == bundle.data["non_weather"]["by_route"]["003"]  # unknown vessel: route rate
    assert 0 < bundle.data["non_weather"]["overall"] < 0.2


def test_foi_and_live_labels_load_together_with_foi_winning(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from ferrypredictor.foi import LABEL_SCHEMA
    from ferrypredictor.labels import LIVE_LABEL_SCHEMA

    def row(key, source, label, day, **extra):
        r = {f: None for f in LABEL_SCHEMA.names}
        r.update(source=source, sailing_key=key, route_code="003", sailing_date=day, departure_local="07:00",
                 departure_at=pd.Timestamp(day, tz="UTC"), label=label, train=True, **extra)
        return r

    d1, d2 = pd.Timestamp("2026-09-30").date(), pd.Timestamp("2026-11-25").date()
    pq.write_table(pa.Table.from_pylist([row("a", "foi4405", "operated", d1)], schema=LABEL_SCHEMA), tmp_path / "foi.parquet")
    live = tmp_path / "labels_live"
    live.mkdir()
    t0, t1 = pd.Timestamp("2026-11-27", tz="UTC"), pd.Timestamp("2026-12-01", tz="UTC")
    rows = [row("a", "live", "cancelled_weather", d1, labeller_version=1, confidence="notice", labelled_at=t0),
            row("b", "live", "operated", d2, labeller_version=1, confidence="timetable", labelled_at=t0),
            row("b", "live", "cancelled_weather", d2, labeller_version=2, confidence="notice", labelled_at=t1)]
    pq.write_table(pa.Table.from_pylist(rows, schema=LIVE_LABEL_SCHEMA), live / "part.parquet")
    out = T.load_labels([tmp_path / "foi.parquet", live]).set_index("sailing_key")
    assert out.loc["a", "source"] == "foi4405" and out.loc["a", "label"] == "operated"
    assert out.loc["b", "label"] == "cancelled_weather"  # newest labeller version
    assert list(out.columns) == [c for c in LABEL_SCHEMA.names if c != "sailing_key"]
