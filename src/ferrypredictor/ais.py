"""ais job: a short listen to aisstream.io around Arran, keeping reports from the Arran vessels."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import websockets

from . import config
from .config import normalise_vessel_name
from .health import update_health
from .storage import append_rows, load_state, save_state

STREAM_URL = "wss://stream.aisstream.io/v0/stream"
MESSAGE_TYPES = ["PositionReport", "ShipStaticData"]
TS = pa.timestamp("us", tz="UTC")

AIS_SCHEMA = pa.schema(
    [
        pa.field("mmsi", pa.int64(), nullable=False),
        pa.field("name", pa.string()),
        pa.field("message_type", pa.string(), nullable=False),
        pa.field("received_at", TS, nullable=False),
        pa.field("reported_at", TS),
        pa.field("lat", pa.float64()),
        pa.field("lon", pa.float64()),
        pa.field("sog", pa.float64()),
        pa.field("cog", pa.float64()),
        pa.field("heading", pa.float64()),
        pa.field("nav_status", pa.int32()),
    ]
)

_TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(?:\.(\d+))? ([+-]\d{4})")


class AisError(RuntimeError):
    pass


def parse_time(text: str | None) -> datetime | None:
    """aisstream times look like '2026-11-23 12:31:05.123456789 +0000 UTC'."""
    m = _TIME_RE.match(text or "")
    if not m:
        return None
    day, clock, frac, offset = m.groups()
    frac = (frac or "0")[:6].ljust(6, "0")
    return datetime.strptime(f"{day} {clock}.{frac} {offset}", "%Y-%m-%d %H:%M:%S.%f %z").astimezone(timezone.utc)


def _num(v):
    return float(v) if isinstance(v, (int, float)) else None


def parse_message(msg: dict, tracked_names: set[str], known_mmsi: dict[int, str], received_at: datetime) -> dict | None:
    meta = msg.get("MetaData") or {}
    mmsi = meta.get("MMSI")
    if not isinstance(mmsi, int):
        return None
    name = normalise_vessel_name(meta.get("ShipName"))
    if mmsi in known_mmsi:
        name = known_mmsi[mmsi]
    elif name not in tracked_names:
        return None
    mtype = msg.get("MessageType", "")
    body = (msg.get("Message") or {}).get(mtype) or {}
    is_position = mtype == "PositionReport"
    nav = body.get("NavigationalStatus") if is_position else None
    return {
        "mmsi": mmsi,
        "name": name,
        "message_type": mtype,
        "received_at": received_at,
        "reported_at": parse_time(meta.get("time_utc")),
        "lat": _num(meta.get("latitude")),
        "lon": _num(meta.get("longitude")),
        "sog": _num(body.get("Sog")) if is_position else None,
        "cog": _num(body.get("Cog")) if is_position else None,
        "heading": _num(body.get("TrueHeading")) if is_position else None,
        "nav_status": nav if isinstance(nav, int) else None,
    }


def discover(records: list[dict], state: dict[str, int], configured: dict[str, int]) -> dict[str, int]:
    """Add name -> MMSI pairs seen on AIS. Names with a configured MMSI are left to the config."""
    out = dict(state)
    for r in records:
        name, mmsi = r.get("name"), r.get("mmsi")
        if name and mmsi and name not in configured:
            out.setdefault(name, mmsi)
    return out


def subscription(api_key: str, bbox: config.BBox) -> dict:
    return {
        "APIKey": api_key,
        "BoundingBoxes": [[[bbox.lat_min, bbox.lon_min], [bbox.lat_max, bbox.lon_max]]],
        "FilterMessageTypes": MESSAGE_TYPES,
    }


async def listen(api_key: str, bbox: config.BBox, seconds: float) -> list[dict]:
    """Connect, subscribe within aisstream's three-second window, and collect messages for `seconds`."""
    messages: list[dict] = []
    try:
        async with websockets.connect(STREAM_URL, open_timeout=15, close_timeout=5) as ws:
            await ws.send(json.dumps(subscription(api_key, bbox)))
            deadline = time.monotonic() + seconds
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                msg = json.loads(raw)
                if "error" in msg:
                    raise AisError(f"aisstream: {msg['error']}")
                messages.append(msg)
    except (OSError, websockets.exceptions.WebSocketException) as e:
        if not messages:
            raise AisError(f"aisstream connection failed: {e}") from e
    return messages


def bbox_override(text: str | None) -> config.BBox | None:
    """'lat_min,lon_min,lat_max,lon_max' from FERRY_AIS_BBOX, for one-off coverage checks."""
    if not text:
        return None
    a, b, c, d = (float(x) for x in text.split(","))
    return config.BBox(a, b, c, d)


def run_ais(
    api_key: str,
    now: datetime,
    data_dir: Path,
    site_dir: Path,
    seconds: float = 180,
    listen_fn=listen,
    bbox: config.BBox | None = None,
) -> dict:
    data_dir, site_dir = Path(data_dir), Path(site_dir)
    state_path = data_dir / "state" / "vessels_mmsi.json"
    vessels = config.load_vessels()
    configured = {v.name: v.mmsi for v in vessels if v.mmsi}
    state = load_state(state_path, {})
    known = {mmsi: name for name, mmsi in {**state, **configured}.items()}
    tracked = {v.name for v in vessels}

    messages = asyncio.run(listen_fn(api_key, bbox or config.load_bbox(), seconds))
    records = [r for m in messages if (r := parse_message(m, tracked, known, now))]

    append_rows(data_dir / "ais", records, AIS_SCHEMA, now)
    new_state = discover(records, state, configured)
    if new_state != state:
        save_state(state_path, new_state)

    per_vessel = dict(sorted(Counter(r["name"] for r in records).items()))
    others = Counter(
        normalise_vessel_name((m.get("MetaData") or {}).get("ShipName")) or str((m.get("MetaData") or {}).get("MMSI"))
        for m in messages
    )
    summary = {
        "messages": len(messages),
        "records": len(records),
        "vessels": per_vessel,
        # Coverage diagnostics: what else the stream delivered in the box.
        "message_types": dict(Counter(m.get("MessageType", "?") for m in messages)),
        "distinct_ships": len(others),
        "top_ships": dict(others.most_common(8)),
    }
    update_health(site_dir, "ais", now, details=summary)
    return summary
