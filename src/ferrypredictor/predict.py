"""predict job: the chance each sailing in the next five days runs.

Runs after every collect. Inputs: the trained bundle (model/weather.json), the latest GFS and
ECMWF 0.25 degree runs from the forecast log, the timetable, the vessel on each route (first listed
in CalMac's route status, unless config/routes.yaml sets `operating_vessel`), and CalMac's word on
each sailing (parsed notices; removal from the timetable).

Lead day: hours from the forecast run to departure, in whole days, clipped to 1-5 (the archive the
model learned from starts at lead day 1, so same-day sailings use the day-1 model).

CalMac first: a sailing CalMac has cancelled (or dropped from its timetable) shows a 0% chance of
running whatever the forecast says. Diversions and warnings are shown alongside the model's figure.

Display (design spec): today and the next two days as percentages; the two days after as bands:
Likely (75% or more), Uncertain (40-74%), Unlikely (below 40%).

Livestock and timber: a red, amber or green light per sailing (cargo.py), from CalMac's notice wording
where it has said, otherwise estimated from the chance it runs.

Output: site/predictions.json every run; data/predictions/ parts when a new forecast run arrives,
so predictions as issued can be scored against what happened.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa

from . import cargo, checks
from . import features as F
from . import parse
from .bundle import Bundle
from .collect import NOTICE_SCHEMA, ROUTE_STATUS_SCHEMA, SAILING_SCHEMA, sailing_key
from .config import REPO_ROOT, normalise_vessel_name
from .forecast import FORECAST_SCHEMA
from .health import update_health
from .storage import append_rows, load_state, read_dataset, save_state

UK = parse.UK
DAYS = 5
PERCENT_DAYS = 3  # today and the next two days
MODEL_PATH = Path(os.environ.get("FERRY_MODEL_PATH", REPO_ROOT / "model" / "weather.json"))
TS = pa.timestamp("us", tz="UTC")
PREDICTION_SCHEMA = pa.schema(
    [
        pa.field("predicted_at", TS, nullable=False),
        pa.field("sailing_key", pa.string(), nullable=False),
        pa.field("route_code", pa.string()),
        pa.field("sailing_date", pa.date32()),
        pa.field("departure_local", pa.string()),
        pa.field("vessel", pa.string()),
        pa.field("lead_hours", pa.float64()),
        pa.field("lead_day", pa.int8()),
        pa.field("p_weather", pa.float64()),
        pa.field("p_non_weather", pa.float64()),
        pa.field("p_runs", pa.float64()),
        pa.field("calmac", pa.string()),
        pa.field("livestock_light", pa.string()),
        pa.field("livestock_source", pa.string()),
        pa.field("gust_run", TS),
        pa.field("second_run", TS),
        pa.field("model_as_of", pa.string()),
    ]
)


def band(p_runs: float | None) -> str | None:
    if p_runs is None or np.isnan(p_runs):
        return None
    pct = round(p_runs * 100)  # judged on the whole percentage the page shows, so 74.8% reads 75% and Likely
    return "Likely" if pct >= 75 else "Uncertain" if pct >= 40 else "Unlikely"


def _hourly(m: pd.DataFrame) -> pd.DataFrame:
    """Fill gaps up to 3 hours (GFS and ECMWF go 3-hourly further out) so every crossing hour has a value,
    as in the hourly archive the model learned from. Speeds interpolate; direction takes the nearest hour."""
    out = []
    for point, g in m.groupby("point"):
        g = g.sort_values("valid_time").drop_duplicates("valid_time").set_index("valid_time")
        idx = pd.date_range(g.index.min(), g.index.max(), freq="h")
        h = g.reindex(idx)
        for col in ("wind_speed_kn", "wind_gust_kn"):
            h[col] = h[col].interpolate(limit=3, limit_area="inside")
        h["wind_dir_deg"] = h["wind_dir_deg"].ffill(limit=1).bfill(limit=2)
        h["point"], h["model"] = point, g["model"].iloc[0]
        out.append(h.rename_axis("valid_time").reset_index())
    return pd.concat(out, ignore_index=True) if out else m


def latest_runs(forecasts: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Latest run of each training model, hourly, as features.build expects (lead_day 0 marks 'live')."""
    runs, parts = {}, []
    for model in (F.GUST_MODEL, F.SECOND_MODEL):
        m = forecasts[forecasts["model"] == model]
        if m.empty:
            continue
        stamp = m["run_time"].fillna(m["fetched_at"])
        newest = stamp.max()
        runs[model] = newest
        parts.append(_hourly(m[stamp == newest][["model", "point", "valid_time", "wind_speed_kn", "wind_gust_kn", "wind_dir_deg"]]))
    f = pd.concat(parts, ignore_index=True) if parts else forecasts.iloc[0:0]
    return f.assign(lead_day=0), runs


def operating_vessels(status_rows: list[dict], overrides: dict[str, str] | None = None) -> dict[str, str]:
    latest: dict[str, dict] = {}
    for r in status_rows:
        if r["route_code"] not in latest or r["observed_at"] > latest[r["route_code"]]["observed_at"]:
            latest[r["route_code"]] = r
    out = {route: normalise_vessel_name((r.get("vessels") or "").split(",")[0]) or None for route, r in latest.items()}
    out.update({k: normalise_vessel_name(v) for k, v in (overrides or {}).items()})
    return out


def _route_overrides() -> dict[str, str]:
    import yaml

    from .config import config_dir

    routes = yaml.safe_load((config_dir() / "routes.yaml").read_text()).get("routes", [])
    return {str(r["code"]): r["operating_vessel"] for r in routes if r.get("operating_vessel")}


def calmac_word(kinds: set[str], removed: bool) -> str | None:
    if "reinstated" in kinds and "cancelled" in kinds:
        kinds = kinds - {"cancelled"}
    if "cancelled" in kinds or removed:
        return "cancelled"
    for k in ("diverted", "at_risk_diversion", "at_risk"):
        if k in kinds:
            return k
    return None


def build(now: datetime, data_dir: Path, bundle: Bundle) -> tuple[list[dict], dict]:
    from .site import active_versions, current_events  # page helpers; no heavy imports

    data_dir = Path(data_dir)
    sail_rows = read_dataset(data_dir / "sailings", SAILING_SCHEMA).to_pylist()
    all_events = read_dataset(data_dir / "events", parse.EVENT_SCHEMA).to_pylist()
    timetable = checks.timetable(sail_rows, all_events)  # removals only count when a notice backs them
    detail = {}
    for r in sail_rows:
        if r.get("departure_at"):
            detail[r.get("sailing_key") or sailing_key(r["route_code"], r["origin"], r["departure_at"])] = r
    vessels = operating_vessels(read_dataset(data_dir / "route_status", ROUTE_STATUS_SCHEMA).to_pylist(), _route_overrides())
    live = active_versions(read_dataset(data_dir / "notices", NOTICE_SCHEMA).to_pylist(), now)
    events = current_events(all_events, live)
    stock = cargo.restrictions(live, timetable)
    forecasts, runs = latest_runs(read_dataset(data_dir / "forecasts", FORECAST_SCHEMA).to_pandas())

    today = now.astimezone(UK).date()
    window = [s for s in timetable if today <= s.date < today + timedelta(days=DAYS)]
    meta = {"model_as_of": bundle.data["as_of"], "data_through": bundle.data["data_through"],
            "runs": {k: v.isoformat() for k, v in runs.items()}}
    if not window:
        return [], meta

    S = pd.DataFrame([{
        "sailing_key": s.key, "route_code": s.route, "origin": s.origin, "destination": s.destination,
        "departure_at": detail.get(s.key, {}).get("departure_at"), "arrival_at": detail.get(s.key, {}).get("arrival_at"),
        "vessel": vessels.get(s.route),
    } for s in window])
    S = S[S["departure_at"].notna()].reset_index(drop=True)
    feats = F.build(S, forecasts, lead_days=(0,)) if not forecasts.empty else pd.DataFrame()

    gust_run = runs.get(F.GUST_MODEL)
    preds = {}
    if not feats.empty and gust_run is not None:
        feats = feats.merge(S[["sailing_key", "route_code", "vessel", "departure_at"]], on="sailing_key")
        hours = (pd.to_datetime(feats["departure_at"], utc=True) - gust_run).dt.total_seconds() / 3600
        feats["lead_hours"] = hours
        feats["lead_day"] = np.clip(np.floor(hours / 24), 1, 5).astype(int)
        ok = F.complete(feats)
        if ok.any():
            p = bundle.predict(feats[ok])
            for (_, f), (_, r) in zip(feats[ok].iterrows(), p.iterrows()):
                preds[f["sailing_key"]] = {"lead_hours": float(f["lead_hours"]), "lead_day": int(f["lead_day"]),
                                           "p_weather": float(r["p_weather"]), "p_non_weather": float(r["p_non_weather"]),
                                           "p_runs": float(r["p_runs"])}

    by_key = {s.key: s for s in window}
    out = []
    for _, row in S.iterrows():
        s = by_key[row["sailing_key"]]
        dep = row["departure_at"]
        word = calmac_word(events.get(s.key, set()), s.removed and s.removal_confirmed)
        p = preds.get(s.key)
        p_runs = 0.0 if word == "cancelled" else (p["p_runs"] if p else None)
        offset = (s.date - today).days
        stock_light = cargo.light(stock.get(s.key), events.get(s.key, set()), word == "cancelled",
                                  p_runs, p["p_weather"] if p else None)
        out.append({
            "key": s.key, "route": s.route, "date": s.date.isoformat(), "depart": s.local,
            "from": s.origin, "to": s.destination, "vessel": row["vessel"],
            "departed": bool(dep < now), "calmac": word,
            "lead_day": p["lead_day"] if p else None, "lead_hours": round(p["lead_hours"], 1) if p else None,
            "p_weather": round(p["p_weather"], 4) if p else None,
            "p_non_weather": round(p["p_non_weather"], 4) if p else None,
            "p_runs": round(p_runs, 4) if p_runs is not None else None,
            "show": "percent" if offset < PERCENT_DAYS else "band",
            "band": band(p_runs) if p_runs is not None else None,
            "livestock": stock_light,
            "missing_from_feed": s.removed and not s.removal_confirmed,
        })
    return out, meta


NEXT_PER_DIRECTION = 6


def next_by_direction(sailings: list[dict], n: int = NEXT_PER_DIRECTION) -> list[dict]:
    """The next few sailings each way, compact enough for a Home Assistant attribute."""
    out = []
    for route, origin, dest in sorted({(s["route"], s["from"], s["to"]) for s in sailings}):
        up = sorted((s for s in sailings if (s["route"], s["from"], s["to"]) == (route, origin, dest) and not s["departed"]),
                    key=lambda s: (s["date"], s["depart"]))[:n]
        out.append({"route": route, "from": origin, "to": dest, "sailings": [
            {"date": s["date"], "time": s["depart"], "vessel": s["vessel"],
             "chance": None if s["p_runs"] is None else round(s["p_runs"] * 100),
             "show": s["show"], "band": s["band"], "calmac": s["calmac"],
             "livestock": (s.get("livestock") or {}).get("light"),
             "livestock_source": (s.get("livestock") or {}).get("source")} for s in up]})
    return out


def run_predict(now: datetime, data_dir: Path, site_dir: Path, model_path: Path = MODEL_PATH) -> dict:
    data_dir, site_dir = Path(data_dir), Path(site_dir)
    if not Path(model_path).exists():
        update_health(site_dir, "predict", now, details={"skipped": f"no model at {model_path}"})
        return {"skipped": "no model"}
    bundle = Bundle.load(model_path)
    sailings, meta = build(now, data_dir, bundle)
    payload = {"generated_at": now.isoformat(timespec="seconds"), **meta,
               "next": next_by_direction(sailings), "sailings": sailings}
    site_dir.mkdir(parents=True, exist_ok=True)
    tmp = site_dir / "predictions.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(site_dir / "predictions.json")

    # Archive predictions once per new forecast run, for scoring against outcomes later.
    state_path = data_dir / "state" / "predict_last.json"
    state = load_state(state_path, {})
    archived = 0
    if meta["runs"] and state.get("runs") != meta["runs"]:
        rows = [{
            "predicted_at": now, "sailing_key": s["key"], "route_code": s["route"],
            "sailing_date": datetime.fromisoformat(s["date"]).date(), "departure_local": s["depart"],
            "vessel": s["vessel"], "lead_hours": s["lead_hours"], "lead_day": s["lead_day"],
            "p_weather": s["p_weather"], "p_non_weather": s["p_non_weather"], "p_runs": s["p_runs"],
            "calmac": s["calmac"],
            "livestock_light": (s.get("livestock") or {}).get("light"),
            "livestock_source": (s.get("livestock") or {}).get("source"),
            "gust_run": datetime.fromisoformat(meta["runs"][F.GUST_MODEL]) if F.GUST_MODEL in meta["runs"] else None,
            "second_run": datetime.fromisoformat(meta["runs"][F.SECOND_MODEL]) if F.SECOND_MODEL in meta["runs"] else None,
            "model_as_of": meta["model_as_of"],
        } for s in sailings if not s["departed"] and s["lead_day"] is not None]
        append_rows(data_dir / "predictions", rows, PREDICTION_SCHEMA, now)
        save_state(state_path, {"runs": meta["runs"]})
        archived = len(rows)

    with_chance = sum(1 for s in sailings if s["p_runs"] is not None)
    summary = {"sailings": len(sailings), "with_chance": with_chance, "archived": archived,
               "model_as_of": meta["model_as_of"], "runs": meta["runs"]}
    update_health(site_dir, "predict", now, details=summary)
    return summary
