"""Static configuration: routes, ports, forecast points, AIS area and tracked vessels."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    return Path(os.environ.get("FERRY_CONFIG_DIR", REPO_ROOT / "config"))


def _load(name: str) -> dict:
    with open(config_dir() / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass(frozen=True)
class Route:
    code: str
    name: str
    ports: tuple[str, str]


@dataclass(frozen=True)
class Point:
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class BBox:
    lat_min: float
    lon_min: float
    lat_max: float
    lon_max: float


@dataclass(frozen=True)
class Vessel:
    name: str
    mmsi: int | None


def normalise_vessel_name(name: str | None) -> str:
    """Upper case, collapse spaces, drop a leading 'MV '."""
    n = " ".join((name or "").upper().split())
    return re.sub(r"^MV\s+", "", n)


def load_routes() -> list[Route]:
    return [Route(str(r["code"]), r["name"], tuple(r["ports"])) for r in _load("routes.yaml")["routes"]]


def load_points() -> dict[str, Point]:
    pts = _load("routes.yaml")["forecast_points"]
    return {k: Point(k, float(v["lat"]), float(v["lon"])) for k, v in pts.items()}


def load_bbox() -> BBox:
    b = _load("routes.yaml")["ais_bbox"]
    return BBox(float(b["lat_min"]), float(b["lon_min"]), float(b["lat_max"]), float(b["lon_max"]))


def load_vessels() -> list[Vessel]:
    out = []
    for v in _load("vessels.yaml")["vessels"]:
        mmsi = v.get("mmsi")
        out.append(Vessel(normalise_vessel_name(v["name"]), int(mmsi) if mmsi else None))
    return out
