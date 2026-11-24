"""Backtest the weather-cancellation model the way it will run live: retrained every month.

For each month of the test winter, every model is trained on all labelled sailings before that
month and scored on that month. Models: climatology, the wind rule, logistic regression, and the
blend (the average of logistic and wind rule), chosen on winter 2024/25 before scoring 2025/26.

Calibration uncertainty: cancellations cluster (one storm day cancels every sailing that day), so
each band's gap gets a 90% interval from resampling whole days. A band "fits" the launch bar if
its interval reaches within 10 percentage points.

Usage: python -m ferrypredictor.backtest LABELS.parquet ARCHIVE_DIR OUT.md [--test 2025-10-01:2026-03-31]
LABELS is the FOI label file (private data repo); ARCHIVE_DIR holds the archived forecasts
(data/forecast_archive on the data branch). The report holds aggregate scores only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

from . import features as F
from .model import CAL_BINS, Climatology, Logistic, WindRule, auc, brier

WEATHER = "cancelled_weather"
OTHER_CANCEL = {"cancelled_technical", "cancelled_other"}
MODELS = ("climatology", "wind_rule", "logistic", "blend")
CHOSEN = "blend"


def load(labels_path: Path, archive_dir: Path) -> pd.DataFrame:
    labels = pd.read_parquet(labels_path)
    labels = labels[labels["train"] & labels["route_code"].isin(["003", "301"])]
    labels = labels[~labels["label"].isin(OTHER_CANCEL)].copy()  # weather model only (see model.py)
    archive = ds.dataset(str(archive_dir), format="parquet").to_table(
        filter=ds.field("model").isin([F.GUST_MODEL, F.SECOND_MODEL])).to_pandas()
    archive = archive[archive["valid_time"] >= labels["departure_at"].min() - pd.Timedelta(days=1)]
    return prepare(labels, archive)


def prepare(labels: pd.DataFrame, archive: pd.DataFrame) -> pd.DataFrame:
    feats = F.build(labels, archive)
    df = feats.merge(labels[["sailing_key", "sailing_date", "label", "vessel"]], on="sailing_key")
    df["y"] = (df["label"] == WEATHER).astype(int)
    df["sailing_date"] = pd.to_datetime(df["sailing_date"])
    df["month"] = df["sailing_date"].dt.month
    return df[F.complete(df)]


def predict_all(train: pd.DataFrame, test: pd.DataFrame) -> dict[str, np.ndarray]:
    pl = Logistic().fit(train, train["y"]).predict(test)
    pw = WindRule().fit(train, train["y"]).predict(test)
    pc = Climatology().fit(train, train["y"]).predict(test)
    return {"climatology": pc, "wind_rule": pw, "logistic": pl, "blend": 0.5 * pl + 0.5 * pw}


def walk_forward(d: pd.DataFrame, start: str, end: str, min_train: int = 500) -> pd.DataFrame:
    """One lead day: monthly retraining across [start, end]. Returns test rows with each model's probability."""
    parts = []
    for m in pd.period_range(start, end, freq="M"):
        train = d[d["sailing_date"] < m.start_time]
        test = d[(d["sailing_date"] >= m.start_time) & (d["sailing_date"] <= m.end_time)]
        if test.empty or len(train) < min_train or train["y"].sum() == 0:
            continue
        preds = predict_all(train, test)
        parts.append(test[["sailing_key", "sailing_date", "y"]].assign(**preds))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def calibration_with_noise(y, p, days, boots: int = 1000, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({"y": np.asarray(y, float), "p": np.asarray(p, float), "day": np.asarray(days)})
    df["bin"] = pd.cut(df["p"], CAL_BINS, right=False)
    t = df.groupby("bin", observed=True).agg(n=("y", "size"), days=("day", "nunique"),
                                             predicted=("p", "mean"), observed=("y", "mean"))
    t["gap"] = t["observed"] - t["predicted"]
    by_day = {k: g for k, g in df.groupby("day")}
    keys = np.array(list(by_day))
    draws = []
    for _ in range(boots):
        s = pd.concat([by_day[k] for k in rng.choice(keys, len(keys))])
        g = s.groupby("bin", observed=False)
        draws.append(g["y"].mean() - g["p"].mean())
    bt = pd.concat(draws, axis=1).reindex(t.index)
    t["gap_lo"], t["gap_hi"] = bt.quantile(0.05, axis=1), bt.quantile(0.95, axis=1)
    t["fits"] = (t["gap_lo"] <= 0.10) & (t["gap_hi"] >= -0.10)
    t["counts"] = t["n"] >= 30
    return t


def run(df: pd.DataFrame, start: str, end: str) -> dict:
    scores, cals = [], {}
    for lead in F.LEAD_DAYS:
        res = walk_forward(df[df["lead_day"] == lead], start, end)
        if res.empty:
            continue
        for m in MODELS:
            scores.append({"lead_day": lead, "model": m, "n": len(res), "cancels": int(res["y"].sum()),
                           "days": res["sailing_date"].nunique(), "brier": brier(res["y"], res[m]), "auc": auc(res["y"], res[m])})
        cals[lead] = calibration_with_noise(res["y"], res[CHOSEN], res["sailing_date"])
    return {"scores": pd.DataFrame(scores), "calibration": cals}


def report(res: dict, start: str, end: str) -> str:
    s = res["scores"]
    b = s.pivot(index="lead_day", columns="model", values="brier")
    a = s.pivot(index="lead_day", columns="model", values="auc")
    first = s.iloc[0]
    lines = [
        "# Backtest: weather cancellations, retrained monthly",
        "",
        f"Test {pd.Timestamp(start):%d %b %Y} to {pd.Timestamp(end):%d %b %Y}: {int(first.n)} sailings on {int(first.days)} days, "
        f"{int(first.cancels)} weather cancellations (lead day 1). Each month uses a model trained on all earlier months.",
        "",
        f"Chosen model: **{CHOSEN}** (average of logistic and wind rule), picked on winter 2024/25.",
        "",
        "| Lead day | Climatology | Wind rule | Logistic | Blend | Blend v climatology | Blend v wind rule | AUC (blend) | Calibration fits, allowing for noise |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    met = []
    for lead in b.index:
        c, w, l, x = (b.loc[lead, k] for k in MODELS)
        cal = res["calibration"][lead]
        fits = bool(cal[cal["counts"]]["fits"].all())
        if lead <= 3:
            met.append(x < min(c, w) and fits)
        lines.append(f"| {lead} | {c:.4f} | {w:.4f} | {l:.4f} | {x:.4f} | {1 - x / c:+.0%} | {1 - x / w:+.0%} | "
                     f"{a.loc[lead, CHOSEN]:.3f} | {'yes' if fits else 'no'} |")
    lines += ["", f"**Launch bar, days 1-3 (beat both baselines; every band within 10 points, allowing for noise): "
              f"{'met' if all(met) else 'not met'}** ({', '.join(f'day {i + 1}: ' + ('met' if ok else 'not met') for i, ok in enumerate(met))}).", ""]
    for lead in sorted(res["calibration"]):
        cal = res["calibration"][lead]
        lines += [f"## Calibration, blend, day {lead}", "",
                  "| Predicted band | Sailings | Days | Predicted | Observed | Gap | 90% interval | Fits |", "|---|---|---|---|---|---|---|---|"]
        for band, r in cal.iterrows():
            lines.append(f"| {band} | {int(r.n)} | {int(r.days)} | {r.predicted:.0%} | {r.observed:.0%} | {r.gap:+.0%} | "
                         f"{r.gap_lo:+.0%} to {r.gap_hi:+.0%} | {'yes' if r.fits else 'NO'}{'' if r.counts else ' (few)'} |")
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("labels", type=Path)
    ap.add_argument("archive", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--test", default="2025-10-01:2026-03-31")
    args = ap.parse_args(argv)
    start, end = args.test.split(":")
    res = run(load(args.labels, args.archive), start, end)
    text = report(res, start, end)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
