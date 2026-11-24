"""Weather-cancellation models and their scoring.

Target: did CalMac cancel the sailing for weather? Sailings cancelled for technical or other reasons
are left out of this model (we can't know whether weather would have stopped them); their risk is a
separate per-vessel base rate. The chance a sailing runs is then (1 - weather) x (1 - non-weather).

Three models, so the logistic model has to earn its place (design spec, launch bar):
- Climatology: the training rate for that route and calendar month.
- WindRule: the training rate for that route and forecast gust band (under 20, 20-29, ... 50+ kn).
- Logistic: logistic regression on the forecast features, one model per lead day.

All three accept sample weights, so recent sailings can count for more: CalMac's behaviour shifts
with each vessel change (Caledonian Isles was away all of 2024; Glen Sannox arrived January 2025).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .features import FEATURES

GUST_BANDS = [0, 20, 30, 40, 50, 1000]
CAL_BINS = [0, 0.05, 0.1, 0.2, 0.35, 0.5, 0.65, 0.8, 1.0001]
PRIOR_N = 20  # pseudo-sailings pulling small groups towards the overall rate


def recency_weights(dates: pd.Series, as_of: pd.Timestamp, half_life_days: float | None) -> np.ndarray | None:
    """Weight 1 for a sailing on as_of, halving every half_life_days before it. None means equal weights."""
    if not half_life_days:
        return None
    age = (as_of - pd.to_datetime(dates)).dt.days.clip(lower=0).to_numpy()
    return 0.5 ** (age / half_life_days)


def _smoothed(groups: pd.DataFrame, overall: float) -> pd.Series:
    return (groups["sum"] + PRIOR_N * overall) / (groups["count"] + PRIOR_N)


def _weighted_groups(y: pd.Series, key: pd.Series, w) -> pd.DataFrame:
    w = pd.Series(1.0 if w is None else np.asarray(w, float), index=y.index)
    return pd.DataFrame({"sum": (y * w).groupby(key).sum(), "count": w.groupby(key).sum()})


def _weighted_mean(y: pd.Series, w) -> float:
    return float(np.average(y, weights=None if w is None else np.asarray(w, float)))


class Climatology:
    name = "climatology"

    def fit(self, X: pd.DataFrame, y: pd.Series, w=None):
        self.overall = _weighted_mean(y, w)
        self.rates = _smoothed(_weighted_groups(y, self._key(X), w), self.overall)
        return self

    @staticmethod
    def _key(X):
        return X["route_troon"].astype(int).astype(str) + "|" + X["month"].astype(str)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return self._key(X).map(self.rates).fillna(self.overall).to_numpy()


class WindRule:
    name = "wind_rule"

    def fit(self, X: pd.DataFrame, y: pd.Series, w=None):
        self.overall = _weighted_mean(y, w)
        self.rates = _smoothed(_weighted_groups(y, self._key(X), w), self.overall)
        return self

    @staticmethod
    def _key(X):
        band = pd.cut(X["gust_max"], GUST_BANDS, right=False, labels=False).fillna(-1).astype(int)
        return X["route_troon"].astype(int).astype(str) + "|" + band.astype(str)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return self._key(X).map(self.rates).fillna(self.overall).to_numpy()


def _design(X: pd.DataFrame) -> pd.DataFrame:
    d = X[FEATURES].copy()
    g = d["gust_max"]
    d["gust_sq"] = g**2 / 100
    d["gust_troon"] = g * d["route_troon"]
    d["north_troon"] = d["gust_north"] * d["route_troon"]
    d["east_troon"] = d["gust_east"] * d["route_troon"]
    return d


class Logistic:
    name = "logistic"

    def __init__(self, C: float = 0.5):
        self.pipe = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                                  LogisticRegression(C=C, max_iter=5000))

    def fit(self, X: pd.DataFrame, y: pd.Series, w=None):
        self.pipe.fit(_design(X), y, logisticregression__sample_weight=None if w is None else np.asarray(w, float))
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return self.pipe.predict_proba(_design(X))[:, 1]

    def coefficients(self) -> dict[str, float]:
        names = list(_design(pd.DataFrame(columns=FEATURES)).columns)
        return dict(zip(names, self.pipe[-1].coef_[0].round(3)))


def brier(y, p) -> float:
    y, p = np.asarray(y, float), np.asarray(p, float)
    return float(np.mean((p - y) ** 2))


def auc(y, p) -> float:
    y, p = np.asarray(y), np.asarray(p, float)
    pos = y.sum()
    if pos in (0, len(y)):
        return float("nan")
    ranks = pd.Series(p).rank().to_numpy()
    return float((ranks[y == 1].sum() - pos * (pos + 1) / 2) / (pos * (len(y) - pos)))


def calibration(y, p, min_n: int = 30) -> pd.DataFrame:
    df = pd.DataFrame({"y": np.asarray(y, float), "p": np.asarray(p, float)})
    df["bin"] = pd.cut(df["p"], CAL_BINS, right=False)
    t = df.groupby("bin", observed=True).agg(n=("y", "size"), predicted=("p", "mean"), observed=("y", "mean"))
    t["gap"] = t["observed"] - t["predicted"]
    t["counts"] = t["n"] >= min_n
    return t


def within_bar(cal: pd.DataFrame, limit: float = 0.10) -> bool:
    """Launch bar: every calibration band with enough sailings within 10 percentage points."""
    counted = cal[cal["counts"]]
    return bool(len(counted)) and bool((counted["gap"].abs() <= limit).all())
