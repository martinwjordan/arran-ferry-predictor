"""Forecast features for each sailing, as the forecast stood N days before it.

One row per (sailing, lead day). The same function serves training (archived forecasts, from the
backfill) and live prediction (the forecast log), so the model always sees the same inputs.

Features, all from the forecast at that lead day:
- gust_max, wind_max: strongest gust and mean wind mid-crossing across the crossing (GFS)
- ecmwf_wind_max: the same from ECMWF 0.25 degree (no gusts archived for it)
- gust_north, gust_east: gust split by the direction it comes from at departure, so the model can
  learn each route's exposure (northerly-easterly winds divert Ardrossan; westerlies cancel)
- port_gust_max: strongest gust at either end of the crossing at departure or arrival
- day_gust_max: the worst gust_max across all of that route's sailings that day (UK date). Once a
  day turns bad, CalMac often calls off the rest of it, borderline sailings included
- route, departure hour (UK), winter flag, vessel
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

UK = "Europe/London"
MID_POINT = {"003": "mid_ardrossan", "301": "mid_troon"}
PORT_POINT = {"Ardrossan": "ardrossan", "Troon": "troon", "Brodick": "brodick"}
GUST_MODEL = "gfs_seamless"
SECOND_MODEL = "ecmwf_ifs025"
LEAD_DAYS = (1, 2, 3, 4, 5)
VESSELS = ("CALEDONIAN ISLES", "ISLE OF ARRAN", "ALFRED", "GLEN SANNOX")
FEATURES = [
    "gust_max", "wind_max", "ecmwf_wind_max", "gust_north", "gust_east", "port_gust_max", "day_gust_max",
    "route_troon", "evening", "winter",
] + [f"vessel_{v.lower().replace(' ', '_')}" for v in VESSELS]


def _hours(dep: pd.Series, arr: pd.Series) -> pd.DataFrame:
    """Expand each sailing into the forecast hours its crossing touches (departure hour to arrival hour)."""
    start = dep.dt.floor("h")
    end = arr.fillna(dep + pd.Timedelta(hours=1)).dt.floor("h")
    n = ((end - start) / pd.Timedelta(hours=1)).clip(lower=0).astype(int) + 1
    idx = np.repeat(np.arange(len(dep)), n.values)
    offs = np.concatenate([np.arange(k) for k in n.values]) if len(n) else np.array([], dtype=int)
    hour = pd.DatetimeIndex(start.dt.tz_convert("UTC").dt.tz_localize(None).values[idx] + pd.to_timedelta(offs, unit="h"))
    return pd.DataFrame({"row": idx, "hour": hour.tz_localize("UTC")})


def build(sailings: pd.DataFrame, forecasts: pd.DataFrame, lead_days=LEAD_DAYS) -> pd.DataFrame:
    """sailings: sailing_key, route_code, origin, destination, departure_at, arrival_at, vessel (UTC times).
    forecasts: model, point, valid_time, lead_day, wind_speed_kn, wind_gust_kn, wind_dir_deg."""
    s = sailings.reset_index(drop=True).copy()
    s["departure_at"] = pd.to_datetime(s["departure_at"], utc=True)
    s["arrival_at"] = pd.to_datetime(s["arrival_at"], utc=True)
    s["mid"] = s["route_code"].map(MID_POINT)
    hours = _hours(s["departure_at"], s["arrival_at"])
    hours["mid"] = s["mid"].values[hours["row"]]
    f = forecasts.copy()
    f["valid_time"] = pd.to_datetime(f["valid_time"], utc=True)

    out = []
    for lead in lead_days:
        fl = f[f["lead_day"] == lead]
        g = fl[fl["model"] == GUST_MODEL][["point", "valid_time", "wind_gust_kn", "wind_speed_kn", "wind_dir_deg"]]
        e = fl[fl["model"] == SECOND_MODEL][["point", "valid_time", "wind_speed_kn"]].rename(columns={"wind_speed_kn": "e_wind"})

        # Mid-crossing, across the crossing window
        h = hours.merge(g, left_on=["mid", "hour"], right_on=["point", "valid_time"], how="left")
        h = h.merge(e, left_on=["mid", "hour"], right_on=["point", "valid_time"], how="left", suffixes=("", "_e"))
        agg = h.groupby("row").agg(gust_max=("wind_gust_kn", "max"), wind_max=("wind_speed_kn", "max"),
                                   ecmwf_wind_max=("e_wind", "max"))

        # Direction at the departure hour, mid-crossing
        dep = s[["mid"]].assign(hour=s["departure_at"].dt.floor("h"), row=s.index)
        dep = dep.merge(g, left_on=["mid", "hour"], right_on=["point", "valid_time"], how="left").set_index("row")
        rad = np.deg2rad(dep["wind_dir_deg"])
        gust_dep = dep["wind_gust_kn"]

        # Port ends: departure port at departure, arrival port at arrival
        ends = []
        for port_col, time_col in (("origin", "departure_at"), ("destination", "arrival_at")):
            t = s[[port_col]].assign(point=s[port_col].map(PORT_POINT), hour=s[time_col].fillna(s["departure_at"]).dt.floor("h"), row=s.index)
            t = t.merge(g[["point", "valid_time", "wind_gust_kn"]], left_on=["point", "hour"], right_on=["point", "valid_time"], how="left")
            ends.append(t.set_index("row")["wind_gust_kn"])
        port = pd.concat(ends, axis=1).max(axis=1)

        local_day = s["departure_at"].dt.tz_convert(UK).dt.date
        local_hour = s["departure_at"].dt.tz_convert(UK).dt.hour
        month = s["departure_at"].dt.tz_convert(UK).dt.month
        feat = pd.DataFrame(
            {
                "sailing_key": s["sailing_key"],
                "lead_day": lead,
                "gust_max": agg["gust_max"],
                "wind_max": agg["wind_max"],
                "ecmwf_wind_max": agg["ecmwf_wind_max"],
                "gust_north": gust_dep * np.cos(rad),
                "gust_east": gust_dep * np.sin(rad),
                "port_gust_max": port,
                "day_gust_max": agg["gust_max"].groupby([s["route_code"], local_day]).transform("max"),
                "route_troon": (s["route_code"] == "301").astype(float),
                "evening": (local_hour >= 16).astype(float),
                "winter": month.isin([10, 11, 12, 1, 2, 3]).astype(float),
            }
        )
        vessel = s.get("vessel", pd.Series([None] * len(s)))
        for v in VESSELS:
            feat[f"vessel_{v.lower().replace(' ', '_')}"] = (vessel == v).astype(float)
        out.append(feat)
    return pd.concat(out, ignore_index=True)


def complete(features: pd.DataFrame) -> pd.Series:
    """Rows with the core forecast inputs present (GFS gust and wind mid-crossing)."""
    return features[["gust_max", "wind_max", "gust_north", "gust_east"]].notna().all(axis=1)


def compass(deg: float) -> str:
    points = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return points[int(((deg % 360) + 22.5) // 45) % 8] if deg is not None and not math.isnan(deg) else "?"
