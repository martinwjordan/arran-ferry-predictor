"""The trained model as plain JSON, and the predictor that uses it.

The JSON holds numbers only (no pickled code), so it can live in the public repo and be read by
anyone checking the method: per lead day, the logistic regression's imputation medians, scaling and
coefficients; the wind rule's gust-band rates; the stacker's weights; and the non-weather rates.

Prediction needs numpy and pandas only. `Bundle.predict` returns, per sailing and lead day:
- p_weather: chance CalMac cancels it for weather
- p_non_weather: chance it is lost to technical, crew or other causes (a rolling rate; a weak prior)
- p_runs: (1 - p_weather) x (1 - p_non_weather). A diverted sailing counts as running.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .features import FEATURES

BUNDLE_VERSION = 1
GUST_BANDS = [0, 20, 30, 40, 50, 1000]


def design(X: pd.DataFrame) -> pd.DataFrame:
    """Model inputs: the features plus a few shaped terms (gust squared; Troon-specific slopes)."""
    d = X[FEATURES].astype(float).copy()
    g = d["gust_max"]
    d["gust_sq"] = g**2 / 100
    d["gust_troon"] = g * d["route_troon"]
    d["north_troon"] = d["gust_north"] * d["route_troon"]
    d["east_troon"] = d["gust_east"] * d["route_troon"]
    return d


def design_columns() -> list[str]:
    return list(design(pd.DataFrame(columns=FEATURES)).columns)


def wind_key(X: pd.DataFrame) -> pd.Series:
    band = pd.cut(X["gust_max"], GUST_BANDS, right=False, labels=False).fillna(-1).astype(int)
    return X["route_troon"].astype(int).astype(str) + "|" + band.astype(str)


def _logit(p) -> np.ndarray:
    p = np.clip(np.asarray(p, float), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def _sigmoid(z) -> np.ndarray:
    return 1 / (1 + np.exp(-np.asarray(z, float)))


class Bundle:
    def __init__(self, data: dict):
        if data.get("version") != BUNDLE_VERSION:
            raise ValueError(f"model bundle version {data.get('version')} is not {BUNDLE_VERSION}")
        self.data = data
        self.leads = {int(k): v for k, v in data["leads"].items()}

    @classmethod
    def load(cls, path: Path) -> "Bundle":
        return cls(json.loads(Path(path).read_text()))

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        tmp.replace(path)

    # --- weather -------------------------------------------------------------------------------
    def logistic(self, X: pd.DataFrame, lead: int) -> np.ndarray:
        m = self.leads[lead]["logistic"]
        D = design(X)[m["columns"]].to_numpy(float)
        med = np.asarray(m["medians"], float)
        D = np.where(np.isnan(D), med, D)
        z = (D - np.asarray(m["means"], float)) / np.asarray(m["scales"], float)
        return _sigmoid(z @ np.asarray(m["coef"], float) + m["intercept"])

    def wind_rule(self, X: pd.DataFrame, lead: int) -> np.ndarray:
        m = self.leads[lead]["wind_rule"]
        return wind_key(X).map(m["rates"]).fillna(m["overall"]).to_numpy(float)

    def weather(self, X: pd.DataFrame, lead: int) -> np.ndarray:
        pl, pw = self.logistic(X, lead), self.wind_rule(X, lead)
        s = self.leads[lead]["stacker"]
        if s is None:
            return 0.5 * pl + 0.5 * pw
        return _sigmoid(s["logistic"] * _logit(pl) + s["wind_rule"] * _logit(pw) + s["intercept"])

    # --- non-weather ---------------------------------------------------------------------------
    def non_weather(self, route: str, vessel: str | None) -> float:
        nw = self.data["non_weather"]
        by_vessel = nw["by_vessel"].get(f"{route}|{vessel}")
        if by_vessel is not None:
            return float(by_vessel)
        return float(nw["by_route"].get(route, nw["overall"]))

    # --- all together --------------------------------------------------------------------------
    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        """X: features (features.build) plus route_code and vessel, any lead days 1-5."""
        out = X[["sailing_key", "lead_day"]].copy()
        out["p_weather"] = np.nan
        for lead in sorted(set(X["lead_day"].astype(int)) & set(self.leads)):
            rows = X["lead_day"].astype(int) == lead
            out.loc[rows, "p_weather"] = self.weather(X[rows], lead)
        out["p_non_weather"] = [self.non_weather(r, v) for r, v in zip(X["route_code"], X.get("vessel", [None] * len(X)))]
        out["p_runs"] = (1 - out["p_weather"]) * (1 - out["p_non_weather"])
        return out
