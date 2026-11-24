"""Scoring live chances against live labels."""

import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ferrypredictor import score as S
from ferrypredictor import storage
from ferrypredictor.labels import LIVE_LABEL_SCHEMA
from ferrypredictor.predict import PREDICTION_SCHEMA

UTC = timezone.utc
NOW = datetime(2026, 12, 20, 9, 0, tzinfo=UTC)


def label(key, day, lab, confidence="timetable", exclusion=None):
    return {"source": "live", "sailing_key": key, "route_code": "003", "sailing_date": day, "departure_local": "07:00",
            "departure_at": datetime(day.year, day.month, day.day, 7, tzinfo=UTC), "arrival_at": None,
            "actual_departure_at": None, "actual_arrival_at": None, "scheduled_leg": "Ardrossan-Brodick",
            "operated_leg": None, "origin": "Ardrossan", "destination": "Brodick", "vessel": "CALEDONIAN ISLES",
            "outcome": lab, "label": lab, "reason_detail": None, "late_reason": None, "delay_minutes": None,
            "train": exclusion is None, "exclusion": exclusion, "labeller_version": 1, "confidence": confidence,
            "labelled_at": NOW}


def prediction(key, day, lead, p_weather, at, calmac=None, p_other=0.05):
    return {"predicted_at": at, "sailing_key": key, "route_code": "003", "sailing_date": day, "departure_local": "07:00",
            "vessel": "CALEDONIAN ISLES", "lead_hours": 24.0 * lead, "lead_day": lead, "p_weather": p_weather,
            "p_non_weather": p_other, "p_runs": 0.0 if calmac == "cancelled" else (1 - p_weather) * (1 - p_other),
            "calmac": calmac, "gust_run": at, "second_run": at, "model_as_of": "2026-10-01"}


def world(n=60, seed=3):
    """Calm days that ran and stormy days that didn't, each forecast a day and three days ahead."""
    rng = np.random.default_rng(seed)
    labels, preds = [], []
    for i in range(n):
        day = date(2026, 11, 24) + timedelta(days=i % 25)
        key = f"003|Ardrossan|{i}"
        stormy = rng.random() < 0.25
        labels.append(label(key, day, "cancelled_weather" if stormy else "operated"))
        for lead in (1, 3):
            at = datetime(day.year, day.month, day.day, 6, tzinfo=UTC) - timedelta(days=lead)
            pw = (0.8 if stormy else 0.05) if lead == 1 else 0.3
            preds.append(prediction(key, day, lead, pw, at - timedelta(hours=6), p_other=0.05))
            preds.append(prediction(key, day, lead, pw, at))  # a later run at the same lead day replaces it
    return pd.DataFrame(labels), pd.DataFrame(preds)


def test_forecast_skill_shows_at_day_one_and_fades_at_day_three():
    labels, preds = world()
    card = S.scorecard(preds, labels, None, NOW)
    d1, d3 = card["by_lead"]["1"], card["by_lead"]["3"]
    assert d1["sailings"] == d3["sailings"] == 60  # one prediction per sailing and lead day
    assert d1["brier"] < d1["brier_assume_sails"] and d1["auc"] > 0.95 and d1["skill_vs_assume_sails"] > 0.5
    assert d3["auc"] == 0.5  # the same chance for everyone carries no information
    assert card["bands"]["Likely"]["ran"] > 0.9 and card["bands"]["Unlikely"]["ran"] == 0


def test_calmac_decisions_and_unscored_labels_are_left_out():
    labels, preds = world(n=40)
    key = labels.iloc[0]["sailing_key"]
    day = labels.iloc[0]["sailing_date"]
    preds = pd.concat([preds, pd.DataFrame([prediction(key, day, 1, 0.05, NOW, calmac="cancelled")])], ignore_index=True)
    labels = pd.concat([labels, pd.DataFrame([label("relief", date(2026, 11, 30), "diverted", exclusion="additional_unscheduled")])])
    card = S.scorecard(preds, labels, None, NOW)
    assert card["left_out_calmac_already_cancelled"] == 1
    assert card["scored_sailings"] == 40  # the relief sailing isn't scored


def test_confident_labels_are_reported_separately_and_misses_listed():
    labels, preds = world(n=40)
    labels.loc[labels.index[:10], "confidence"] = "removal"
    labels.loc[labels.index[0], "label"] = "operated"  # a calm call... flip one to a confident miss
    preds.loc[preds["sailing_key"] == labels.iloc[1]["sailing_key"], "p_weather"] = 0.01
    labels.loc[labels.index[1], "label"] = "cancelled_weather"
    card = S.scorecard(preds, labels, None, NOW)
    assert card["confident_only"]["by_lead"]["1"]["sailings"] == 30
    assert any(m["label"] == "cancelled_weather" and m["chance"] > 0.9 for m in card["misses"])


def test_run_writes_json_and_markdown(tmp_path):
    labels, preds = world(n=40)
    data, site = tmp_path / "data", tmp_path / "site"
    storage.append_rows(data / "labels_live", labels.to_dict("records"), LIVE_LABEL_SCHEMA, NOW)
    storage.append_rows(data / "predictions", preds.to_dict("records"), PREDICTION_SCHEMA, NOW)
    out = S.run_score(NOW, data, site, tmp_path / "no-model.json")
    assert out["scored"] == 40
    card = json.loads((site / "scorecard.json").read_text())
    assert card["by_lead"]["1"]["brier_climatology"] is None  # no model to take climatology from
    assert "| 1 | 40 |" in (site / "scorecard.md").read_text()


def test_nothing_to_score_yet(tmp_path):
    out = S.run_score(NOW, tmp_path / "data", tmp_path / "site", tmp_path / "no-model.json")
    assert out == {"labelled": 0, "scored": 0, "labels_through": None}
    assert "Nothing to score yet" in (tmp_path / "site" / "scorecard.md").read_text()
