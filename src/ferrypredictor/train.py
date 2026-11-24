"""Train the model on everything up to a cut-off and write it as a JSON bundle.

Usage: python -m ferrypredictor.train LABELS.parquet ARCHIVE_DIR OUT.json [--as-of 2026-10-01]

The same method the backtest scores: per lead day, logistic regression and the wind rule fitted on
all labelled sailings before the cut-off, and a stacker fitted on their out-of-time predictions
(each month predicted by models trained on the months before it). Non-weather risk: the rate of
technical and other cancellations over the last NON_WEATHER_DAYS days, per route, and per vessel
where it has enough sailings (window chosen on winter 2024/25; it is a weak prior, because these
cancellations come in episodes such as a vessel breakdown, which CalMac's notices report directly).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import backtest as B
from . import features as F
from .bundle import BUNDLE_VERSION, Bundle, design_columns
from .model import Logistic, Stacker, WindRule

NON_WEATHER_DAYS = 180
MIN_VESSEL_SAILINGS = 200
PRIOR_N = 50
MIN_TRAIN = 500
NON_WEATHER = {"cancelled_technical", "cancelled_other"}


def non_weather_rates(labels: pd.DataFrame, as_of: pd.Timestamp, days: int = NON_WEATHER_DAYS) -> dict:
    L = labels[labels["train"] & labels["route_code"].isin(["003", "301"])].copy()
    L["date"] = pd.to_datetime(L["sailing_date"])
    L = L[(L["date"] < as_of) & (L["date"] >= as_of - pd.Timedelta(days=days))]
    L["y"] = L["label"].isin(NON_WEATHER).astype(int)
    overall = float(L["y"].mean()) if len(L) else 0.03
    route = L.groupby("route_code")["y"].agg(["sum", "count"])
    by_route = ((route["sum"] + PRIOR_N * overall) / (route["count"] + PRIOR_N)).round(4).to_dict()
    by_vessel = {}
    for (r, v), g in L.groupby(["route_code", "vessel"])["y"]:
        if len(g) >= MIN_VESSEL_SAILINGS:
            base = by_route.get(r, overall)
            by_vessel[f"{r}|{v}"] = round(float((g.sum() + PRIOR_N * base) / (len(g) + PRIOR_N)), 4)
    return {"window_days": days, "from": str((as_of - pd.Timedelta(days=days)).date()), "to": str(as_of.date()),
            "sailings": int(len(L)), "overall": round(overall, 4), "by_route": by_route, "by_vessel": by_vessel}


def _logistic_params(m: Logistic) -> dict:
    imputer, scaler, lr = m.pipe[0], m.pipe[1], m.pipe[-1]
    return {"columns": design_columns(), "medians": imputer.statistics_.round(6).tolist(),
            "means": scaler.mean_.round(6).tolist(), "scales": scaler.scale_.round(6).tolist(),
            "coef": lr.coef_[0].round(6).tolist(), "intercept": round(float(lr.intercept_[0]), 6)}


def fit(df: pd.DataFrame, labels: pd.DataFrame, as_of: pd.Timestamp, history_start: str = B.HISTORY_START) -> Bundle:
    leads = {}
    train_df = df[df["sailing_date"] < as_of]
    for lead in F.LEAD_DAYS:
        d = train_df[train_df["lead_day"] == lead]
        if len(d) < MIN_TRAIN or d["y"].sum() == 0:
            continue  # not enough history for this lead day; predict leaves it blank
        logistic = Logistic().fit(d, d["y"])
        wind = WindRule().fit(d, d["y"])
        last_month = (as_of - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        oot = B.walk_forward(d, history_start, last_month)
        stacker = Stacker().fit(oot["logistic"], oot["wind_rule"], oot["y"]) if not oot.empty else Stacker()
        leads[str(lead)] = {
            "sailings": int(len(d)), "weather_cancellations": int(d["y"].sum()),
            "oot_sailings": int(len(oot)),
            "logistic": _logistic_params(logistic),
            "wind_rule": {"rates": {k: round(float(v), 6) for k, v in wind.rates.items()}, "overall": round(wind.overall, 6)},
            "stacker": stacker.weights(),
        }
    data = {
        "version": BUNDLE_VERSION,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": str(as_of.date()),
        "data_from": str(train_df["sailing_date"].min().date()),
        "data_through": str(train_df["sailing_date"].max().date()),
        "features": list(F.FEATURES),
        "forecast_models": {"gust": F.GUST_MODEL, "second": F.SECOND_MODEL},
        "leads": leads,
        "non_weather": non_weather_rates(labels, as_of),
        "method": "Logistic regression and gust-band rule per lead day, combined by a stacker fitted on "
                  "out-of-time predictions; trained on CalMac FOI 4405 outcomes and Open-Meteo archived forecasts.",
    }
    return Bundle(data)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("labels", type=Path)
    ap.add_argument("archive", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--as-of", default=None, help="train on sailings before this date (default: day after the last label)")
    args = ap.parse_args(argv)
    df = B.load(args.labels, args.archive)
    labels = pd.read_parquet(args.labels)
    as_of = pd.Timestamp(args.as_of) if args.as_of else df["sailing_date"].max() + pd.Timedelta(days=1)
    bundle = fit(df, labels, as_of)
    bundle.save(args.out)
    d = bundle.data
    print({"as_of": d["as_of"], "data_through": d["data_through"],
           "stacked_leads": [k for k, v in d["leads"].items() if v["stacker"]],
           "non_weather": d["non_weather"]["by_route"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
