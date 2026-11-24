"""Features, models and backtest, on made-up sailings and forecasts."""

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from ferrypredictor import backtest as B
from ferrypredictor import features as F
from ferrypredictor import model as M

UTC = timezone.utc


def forecasts_for(hours, leads=(1, 2), gust=30.0, wind=20.0, direction=90.0, points=None):
    points = points or ["mid_ardrossan", "mid_troon", "ardrossan", "troon", "brodick"]
    rows = []
    for lead in leads:
        for p in points:
            for h in hours:
                g = gust(h, p) if callable(gust) else gust
                rows.append({"model": F.GUST_MODEL, "point": p, "valid_time": h, "lead_day": lead,
                             "wind_speed_kn": wind, "wind_gust_kn": g, "wind_dir_deg": direction})
                rows.append({"model": F.SECOND_MODEL, "point": p, "valid_time": h, "lead_day": lead,
                             "wind_speed_kn": wind + 1, "wind_gust_kn": None, "wind_dir_deg": direction})
    return pd.DataFrame(rows)


def sailing(key, route, origin, dest, dep, arr, vessel="CALEDONIAN ISLES"):
    return {"sailing_key": key, "route_code": route, "origin": origin, "destination": dest,
            "departure_at": dep, "arrival_at": arr, "vessel": vessel}


def test_features_take_the_worst_hour_of_the_crossing_and_split_direction():
    dep = datetime(2025, 1, 10, 7, 0, tzinfo=UTC)
    hours = [dep + timedelta(hours=k) for k in range(-1, 4)]
    gust = lambda h, p: 50.0 if (p == "mid_ardrossan" and h.hour == 7) else 25.0
    s = pd.DataFrame([sailing("a", "003", "Ardrossan", "Brodick", dep, dep + timedelta(minutes=55))])
    f = F.build(s, forecasts_for(hours, gust=gust, direction=90.0), lead_days=(1,))
    r = f.iloc[0]
    assert r["gust_max"] == 50.0 and r["wind_max"] == 20.0 and r["ecmwf_wind_max"] == 21.0
    assert r["gust_east"] == pytest.approx(50.0) and abs(r["gust_north"]) < 1e-9  # easterly
    assert r["port_gust_max"] == 25.0
    assert r["route_troon"] == 0 and r["winter"] == 1 and r["vessel_caledonian_isles"] == 1
    assert F.complete(f).all()


def test_features_use_uk_time_for_evening_and_cover_long_crossings():
    dep = datetime(2025, 7, 1, 15, 30, tzinfo=UTC)  # 16:30 BST
    hours = [dep.replace(minute=0) + timedelta(hours=k) for k in range(0, 3)]
    gust = lambda h, p: 44.0 if h.hour == 16 else 20.0  # only the second hour of the crossing
    s = pd.DataFrame([sailing("t", "301", "Troon", "Brodick", dep, dep + timedelta(minutes=80), vessel="ALFRED")])
    r = F.build(s, forecasts_for(hours, gust=gust), lead_days=(1,)).iloc[0]
    assert r["evening"] == 1 and r["winter"] == 0 and r["route_troon"] == 1 and r["vessel_alfred"] == 1
    assert r["gust_max"] == 44.0


def test_missing_forecast_is_incomplete():
    dep = datetime(2025, 1, 10, 7, 0, tzinfo=UTC)
    s = pd.DataFrame([sailing("a", "003", "Ardrossan", "Brodick", dep, dep + timedelta(minutes=55))])
    f = F.build(s, forecasts_for([dep - timedelta(days=3)]), lead_days=(1,))
    assert not F.complete(f).any()


def synthetic(n_days=540, seed=1):
    """Sailings whose weather cancellations depend on gust, with forecast features already built."""
    rng = np.random.default_rng(seed)
    rows = []
    start = date(2024, 1, 1)
    for d in range(n_days):
        day = start + timedelta(days=d)
        day_gust = rng.gamma(4, 6)
        day_dir = rng.uniform(0, 2 * np.pi)
        for k in range(6):
            g = max(0.0, day_gust + rng.normal(0, 3))
            p = 1 / (1 + np.exp(-(g - 38) / 4))
            rows.append({"sailing_key": f"{day}|{k}", "lead_day": 1, "gust_max": g, "wind_max": g * 0.7,
                         "ecmwf_wind_max": g * 0.7, "gust_north": g * np.cos(day_dir), "gust_east": g * np.sin(day_dir),
                         "port_gust_max": g * 0.9, "day_gust_max": day_gust + 6, "route_troon": float(k % 2), "evening": float(k > 3),
                         "winter": float(day.month in (10, 11, 12, 1, 2, 3)),
                         **{f"vessel_{v.lower().replace(' ', '_')}": float(v == "ISLE OF ARRAN") for v in F.VESSELS},
                         "sailing_date": pd.Timestamp(day), "month": day.month, "y": int(rng.random() < p)})
    return pd.DataFrame(rows)


def test_baselines_and_logistic_learn_the_gust_effect():
    df = synthetic()
    calm, storm = df[df.gust_max < 15].head(50), df[df.gust_max > 50].head(50)
    for cls in (M.WindRule, M.Logistic):
        m = cls().fit(df, df.y)
        assert m.predict(storm).mean() > 0.6 > 0.1 > m.predict(calm).mean()  # the rule's top band starts at 40 kn
    clim = M.Climatology().fit(df, df.y).predict(df)
    assert 0 < clim.min() <= clim.max() < 1
    assert "gust_sq" in M.Logistic().fit(df, df.y).coefficients()


def test_recency_weights_halve_each_half_life():
    as_of = pd.Timestamp("2025-01-01")
    w = M.recency_weights(pd.Series(pd.to_datetime(["2025-01-01", "2024-07-05", "2024-01-07"])), as_of, 180)
    assert w == pytest.approx([1.0, 0.5, 0.25], abs=0.01)
    assert M.recency_weights(pd.Series([as_of]), as_of, None) is None
    df = synthetic()
    assert M.Logistic().fit(df, df.y, M.recency_weights(df.sailing_date, as_of, 180)).predict(df).shape == (len(df),)


def test_scores_and_calibration():
    assert M.brier([0, 1], [0, 1]) == 0 and M.brier([0, 1], [1, 0]) == 1
    assert M.auc(np.array([0, 0, 1, 1]), np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    cal = M.calibration(np.array([0] * 40 + [1] * 40), np.array([0.02] * 40 + [0.95] * 40))
    assert M.within_bar(cal)
    cal = M.calibration(np.array([0] * 40 + [1] * 40), np.array([0.02] * 40 + [0.7] * 40))
    assert not M.within_bar(cal)  # 30 points out


def test_walk_forward_retrains_monthly_and_the_blend_sits_between():
    df = synthetic()
    res = B.walk_forward(df, "2025-01-01", "2025-06-30")
    assert set(B.MODELS) - {"stacked"} <= set(res.columns) and res["sailing_date"].min() >= pd.Timestamp("2025-01-01")
    assert np.allclose(res["blend"], 0.5 * (res["logistic"] + res["wind_rule"]))
    out = B.run(df, "2025-01-01", "2025-06-30")
    s = out["scores"]
    blend, clim = (s[(s.model == m)]["brier"].iloc[0] for m in ("blend", "climatology"))
    assert blend < clim
    text = B.report(out, "2025-01-01", "2025-06-30")
    assert "Launch bar" in text and "90% interval" in text


def test_day_gust_is_the_worst_sailing_of_that_route_and_uk_day():
    d0 = datetime(2025, 1, 10, 7, 0, tzinfo=UTC)
    sails = [sailing("a", "003", "Ardrossan", "Brodick", d0, d0 + timedelta(minutes=55)),
             sailing("b", "003", "Brodick", "Ardrossan", d0 + timedelta(hours=10), d0 + timedelta(hours=10, minutes=55)),
             sailing("c", "301", "Troon", "Brodick", d0 + timedelta(hours=3), d0 + timedelta(hours=4, minutes=20)),
             sailing("d", "003", "Ardrossan", "Brodick", d0 + timedelta(days=1), d0 + timedelta(days=1, minutes=55))]
    hours = [d0 + timedelta(hours=k) for k in range(-1, 30)]
    gust = lambda h, p: 55.0 if (p == "mid_ardrossan" and h == d0 + timedelta(hours=10)) else 20.0
    f = F.build(pd.DataFrame(sails), forecasts_for(hours, gust=gust), lead_days=(1,)).set_index("sailing_key")
    assert f.loc["a", "day_gust_max"] == 55.0 and f.loc["b", "day_gust_max"] == 55.0  # same route, same day
    assert f.loc["c", "day_gust_max"] == 20.0  # Troon route
    assert f.loc["d", "day_gust_max"] == 20.0  # next day


def test_stacker_learns_to_trust_the_sharper_input_and_falls_back_with_little_data():
    rng = np.random.default_rng(3)
    n = 4000
    truth = rng.random(n)
    y = (rng.random(n) < truth).astype(int)
    sharp = np.clip(truth + rng.normal(0, 0.03, n), 0.001, 0.999)  # close to the truth
    flat = np.clip(0.5 * (truth + rng.normal(0, 0.25, n)) + 0.25, 0.01, 0.99)  # noisy and compressed
    s = M.Stacker().fit(sharp, flat, y)
    w = s.weights()
    assert w["logistic"] > abs(w["wind_rule"])
    p = s.predict(sharp, flat)
    assert M.brier(y, p) < M.brier(y, 0.5 * sharp + 0.5 * flat)
    few = M.Stacker().fit(sharp[:50], flat[:50], np.r_[np.ones(5), np.zeros(45)])
    assert few.weights() is None
    assert np.allclose(few.predict(sharp[:3], flat[:3]), 0.5 * sharp[:3] + 0.5 * flat[:3])


def test_stacked_column_uses_only_earlier_months():
    df = synthetic()
    oot = B.walk_forward(df, "2024-07-01", "2025-06-30")
    res = B.stack(oot, "2025-01-01", "2025-06-30")
    assert res["sailing_date"].min() >= pd.Timestamp("2025-01-01") and "stacked" in res
    # Changing a later month's outcomes must not change an earlier month's stacked predictions
    tampered = oot.copy()
    tampered.loc[tampered["sailing_date"] >= pd.Timestamp("2025-03-01"), "y"] = 1
    again = B.stack(tampered, "2025-01-01", "2025-06-30")
    jan = res["sailing_date"] < pd.Timestamp("2025-02-01")
    assert np.allclose(res.loc[jan, "stacked"], again.loc[jan.values, "stacked"])
