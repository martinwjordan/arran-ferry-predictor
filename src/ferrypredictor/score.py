"""score job: how the live chances did, once the live labels say what happened.

For each labelled sailing and lead day (1 to 5), the chance from the latest forecast run at that lead
day is scored against whether the sailing ran (operated or diverted). The model's own chance is used,
(1 - weather risk) x (1 - technical/other risk), and predictions made after CalMac had already
cancelled the sailing are left out: the page shows those as 0% whatever the model says, so they
would flatter the score. Planned timetable changes and late-added relief sailings are not scored.

Scores: Brier (lower is better) against two baselines, "assume it sails" (always 100%) and
climatology (the model's own long-run cancellation rates for the route, with no forecast), plus
AUC and how often sailings in each band ran. Everything is reported for all labels and for
confident labels only (notice or plain timetable), because removal-only and "at risk" labels are
sometimes diversions in disguise. Writes site/scorecard.json and site/scorecard.md.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .bundle import Bundle
from .health import update_health
from .labels import CONFIDENT, NOT_SCORED, RAN, read_live_labels
from .predict import MODEL_PATH, PREDICTION_SCHEMA
from .storage import read_dataset

LEADS = (1, 2, 3, 4, 5)
MIN_FOR_SKILL = 30
MISSES_SHOWN = 15


def band_of(p: float) -> str:
    pct = round(p * 100)
    return "Likely" if pct >= 75 else "Uncertain" if pct >= 40 else "Unlikely"


def auc(y: np.ndarray, p: np.ndarray) -> float | None:
    pos, neg = p[y == 1], p[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None
    ranks = pd.Series(np.concatenate([pos, neg])).rank().to_numpy()
    return float((ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def joined(predictions: pd.DataFrame, labels: pd.DataFrame, bundle: Bundle | None) -> tuple[pd.DataFrame, int]:
    """One row per labelled sailing and lead day: the model's chance from the latest run at that lead day."""
    labels = labels[~labels["exclusion"].isin(NOT_SCORED)]
    p = predictions[predictions["lead_day"].isin(LEADS) & predictions["p_weather"].notna()]
    p = p[p["calmac"] != "cancelled"].sort_values("predicted_at").drop_duplicates(["sailing_key", "lead_day"], keep="last")
    df = p.merge(labels[["sailing_key", "label", "confidence"]], on="sailing_key")
    df["p"] = (1 - df["p_weather"]) * (1 - df["p_non_weather"])
    df["ran"] = df["label"].isin(RAN).astype(int)
    if bundle is not None:
        nw = bundle.data["non_weather"]
        df["p_clim"] = [(1 - bundle.leads[int(l)]["wind_rule"]["overall"]) * (1 - nw["by_route"].get(r, nw["overall"]))
                        for l, r in zip(df["lead_day"], df["route_code"])]
    else:
        df["p_clim"] = np.nan
    decided_keys = set(predictions.loc[predictions["calmac"] == "cancelled", "sailing_key"])
    return df, len(decided_keys & set(labels["sailing_key"]))


def summary(df: pd.DataFrame) -> dict:
    out = {}
    for lead in LEADS:
        d = df[df["lead_day"] == lead]
        if d.empty:
            continue
        y, p = d["ran"].to_numpy(), d["p"].to_numpy()
        brier = float(np.mean((p - y) ** 2))
        b_sails = float(np.mean((1 - y) ** 2))
        b_clim = float(np.mean((d["p_clim"].to_numpy() - y) ** 2)) if d["p_clim"].notna().all() else None
        enough = len(d) >= MIN_FOR_SKILL
        out[str(lead)] = {
            "sailings": int(len(d)), "ran": round(float(y.mean()), 4), "mean_chance": round(float(p.mean()), 4),
            "brier": round(brier, 4), "brier_assume_sails": round(b_sails, 4),
            "brier_climatology": round(b_clim, 4) if b_clim is not None else None,
            "skill_vs_assume_sails": round(1 - brier / b_sails, 3) if enough and b_sails > 0 else None,
            "skill_vs_climatology": round(1 - brier / b_clim, 3) if enough and b_clim else None,
            "auc": (round(a, 3) if (a := auc(y, p)) is not None else None),
        }
    return out


def bands(df: pd.DataFrame) -> dict:
    out = {}
    for name in ("Likely", "Uncertain", "Unlikely"):
        d = df[df["p"].map(band_of) == name]
        out[name] = {"sailings": int(len(d)), "ran": round(float(d["ran"].mean()), 3) if len(d) else None,
                     "mean_chance": round(float(d["p"].mean()), 3) if len(d) else None}
    return out


def misses(df: pd.DataFrame) -> list[dict]:
    """Confident calls that went the other way, closest lead day first, newest first."""
    wrong = df[((df["p"] >= 0.745) & (df["ran"] == 0)) | ((df["p"] < 0.395) & (df["ran"] == 1))]
    wrong = wrong.sort_values(["sailing_date", "departure_local", "lead_day"], ascending=[False, False, True])
    wrong = wrong.drop_duplicates("sailing_key").head(MISSES_SHOWN)
    return [{"date": str(r.sailing_date), "route": r.route_code, "depart": r.departure_local, "vessel": r.vessel,
             "lead_day": int(r.lead_day), "chance": round(float(r.p), 3), "label": r.label, "confidence": r.confidence}
            for r in wrong.itertuples()]


def scorecard(predictions: pd.DataFrame, labels: pd.DataFrame, bundle: Bundle | None, now: datetime) -> dict:
    df, already = joined(predictions, labels, bundle)
    confident = df[df["confidence"].isin(CONFIDENT)]
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "labels_from": str(labels["sailing_date"].min()) if len(labels) else None,
        "labels_through": str(labels["sailing_date"].max()) if len(labels) else None,
        "labelled_sailings": int(len(labels)),
        "scored_sailings": int(df["sailing_key"].nunique()) if len(df) else 0,
        "left_out_calmac_already_cancelled": already,
        "by_lead": summary(df) if len(df) else {},
        "bands": bands(df) if len(df) else {},
        "confident_only": {"by_lead": summary(confident) if len(confident) else {},
                           "bands": bands(confident) if len(confident) else {}},
        "misses": misses(df) if len(df) else [],
    }


def markdown(card: dict) -> str:
    lines = [f"# Live scorecard", "",
             f"Built {card['generated_at']}. Labels {card['labels_from']} to {card['labels_through']}: "
             f"{card['labelled_sailings']} sailings labelled, {card['scored_sailings']} scored. "
             f"{card['left_out_calmac_already_cancelled']} predictions left out because CalMac had already cancelled.", ""]
    if not card["by_lead"]:
        return "\n".join(lines + ["Nothing to score yet."]) + "\n"
    lines += ["| Lead day | Sailings | Ran | Mean chance | Brier | Assume sails | Climatology | Skill vs climatology | AUC |",
              "|---|---|---|---|---|---|---|---|---|"]
    for lead, s in card["by_lead"].items():
        fmt = lambda v: "–" if v is None else v
        lines.append(f"| {lead} | {s['sailings']} | {s['ran']:.0%} | {s['mean_chance']:.0%} | {s['brier']} | "
                     f"{s['brier_assume_sails']} | {fmt(s['brier_climatology'])} | {fmt(s['skill_vs_climatology'])} | {fmt(s['auc'])} |")
    lines += ["", "| Band | Sailings | Ran | Mean chance |", "|---|---|---|---|"]
    for name, b in card["bands"].items():
        ran = "–" if b["ran"] is None else f"{b['ran']:.0%}"
        mean = "–" if b["mean_chance"] is None else f"{b['mean_chance']:.0%}"
        lines.append(f"| {name} | {b['sailings']} | {ran} | {mean} |")
    if card["misses"]:
        lines += ["", "Confident calls that went the other way:", ""]
        lines += [f"- {m['date']} {m['depart']} route {m['route']} ({m['vessel'] or 'vessel unknown'}): "
                  f"{m['chance']:.0%} at day {m['lead_day']}, {m['label']} ({m['confidence']})" for m in card["misses"]]
    lines += ["", f"Skill is shown from {MIN_FOR_SKILL} sailings per lead day. Brier: lower is better."]
    return "\n".join(lines) + "\n"


def run_score(now: datetime, data_dir: Path, site_dir: Path, model_path: Path = MODEL_PATH) -> dict:
    import json

    data_dir, site_dir = Path(data_dir), Path(site_dir)
    labels = pd.DataFrame(read_live_labels(data_dir))
    predictions = read_dataset(data_dir / "predictions", PREDICTION_SCHEMA).to_pandas()
    try:
        bundle = Bundle.load(model_path)
    except (OSError, ValueError):
        bundle = None
    if labels.empty:
        card = scorecard(predictions.iloc[0:0], pd.DataFrame(columns=["sailing_key", "sailing_date", "exclusion", "label", "confidence"]), bundle, now)
    else:
        card = scorecard(predictions, labels, bundle, now)
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "scorecard.json").write_text(json.dumps(card, indent=1))
    (site_dir / "scorecard.md").write_text(markdown(card))
    result = {"labelled": card["labelled_sailings"], "scored": card["scored_sailings"],
              "labels_through": card["labels_through"]}
    update_health(site_dir, "score", now, details=result)
    return result
