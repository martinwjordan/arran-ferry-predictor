"""Turn CalMac's FOI per-sailing performance data into training labels.

Input: the FOI 4405 spreadsheet (sheet "Export"; one row per scheduled sailing on Ardrossan-Brodick,
including Troon legs and diversions, 2007 onwards), and optionally the FOI 4202 cancellations CSV,
whose "Dominant Reason" codes are finer than FOI 4405's weather/technical/other flags.

The raw files are CalMac's and are kept in the private data repo, not here. This module only holds
the code, tested on made-up rows.

Output: one row per sailing, keyed like the live log (route | origin | departure in UTC), with:
- label: operated, cancelled_weather, cancelled_technical, cancelled_other, diverted, returned_to_port
- train: whether the row belongs in training, and exclusion (why not), for
  "Not Operated" placeholders (including "MV Isle of Arran Dummy"), "Additional" unscheduled sailings,
  Covid-era "SCOT GOVERNMENT APPROVED" timetable cuts, and legs that are not Arran routes.

Usage: python -m ferrypredictor.foi ANNEX_E.xlsx [ANNEX_D.csv] OUT.parquet
"""

from __future__ import annotations

import csv
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from .collect import sailing_key
from .config import normalise_vessel_name

UK = ZoneInfo("Europe/London")
SOURCE = "CalMac FOI 4405 (via FOI 4428)"
ARRAN_LEGS = {
    "Ardrossan-Brodick": "003", "Brodick-Ardrossan": "003",
    "Troon-Brodick": "301", "Brodick-Troon": "301",
}
EXCLUDED_REASONS = {"SCOT GOVERNMENT APPROVED"}  # Covid-era planned timetable reductions, not disruption
TS = pa.timestamp("us", tz="UTC")

LABEL_SCHEMA = pa.schema(
    [
        pa.field("source", pa.string(), nullable=False),
        pa.field("sailing_key", pa.string()),
        pa.field("route_code", pa.string()),
        pa.field("sailing_date", pa.date32(), nullable=False),
        pa.field("departure_local", pa.string(), nullable=False),
        pa.field("departure_at", TS),
        pa.field("arrival_at", TS),
        pa.field("actual_departure_at", TS),
        pa.field("actual_arrival_at", TS),
        pa.field("scheduled_leg", pa.string()),
        pa.field("operated_leg", pa.string()),
        pa.field("origin", pa.string()),
        pa.field("destination", pa.string()),
        pa.field("vessel", pa.string()),
        pa.field("outcome", pa.string()),
        pa.field("label", pa.string(), nullable=False),
        pa.field("reason_detail", pa.string()),
        pa.field("late_reason", pa.string()),
        pa.field("delay_minutes", pa.float64()),
        pa.field("train", pa.bool_(), nullable=False),
        pa.field("exclusion", pa.string()),
    ]
)


def _as_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v.strip():
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(v.strip()[:10], fmt).date()
            except ValueError:
                pass
    return None


def _as_time(v) -> time | None:
    if isinstance(v, datetime):
        return v.time()
    if isinstance(v, time):
        return v
    if isinstance(v, timedelta):
        return (datetime.min + v).time()
    if isinstance(v, str) and v.strip():
        for fmt in ("%H:%M:%S", "%H:%M", "%I:%M %p"):
            try:
                return datetime.strptime(v.strip(), fmt).time()
            except ValueError:
                pass
    return None


def uk_to_utc(d: date | None, t: time | None) -> datetime | None:
    """UK wall-clock time to UTC. Clocks-change edge cases resolve to the first occurrence."""
    if d is None or t is None:
        return None
    return datetime.combine(d, t).replace(tzinfo=UK).astimezone(timezone.utc)


def _flag(row: dict, column: str) -> bool:
    try:
        return int(row.get(column) or 0) > 0
    except (TypeError, ValueError):
        return False


def label_for(row: dict) -> str:
    if _flag(row, "Cancelled (Weather)"):
        return "cancelled_weather"
    if _flag(row, "Cancelled (Technical)"):
        return "cancelled_technical"
    if _flag(row, "Cancelled (Other)") or (row.get("Contractual Outcome") == "Cancelled"):
        return "cancelled_other"
    if _flag(row, "Returned to Port Sailings"):
        return "returned_to_port"
    scheduled, operated = row.get("Scheduled Leg"), row.get("Operated Leg")
    if _flag(row, "Diverted Sailings") or (operated and scheduled and operated != scheduled):
        return "diverted"
    return "operated"


def late_reason(row: dict) -> str | None:
    for column, name in (("Late (Weather)", "weather"), ("Late (Tech)", "technical"), ("Late (Other)", "other")):
        if _flag(row, column):
            return name
    return None


def normalise(rows: list[dict], reasons: dict[tuple[date, str], set[str]] | None = None) -> list[dict]:
    """reasons: FOI 4202 'Dominant Reason' codes by (date, HH:MM). FOI 4202 has no direction, and in
    two-vessel timetables sailings leave both ends at the same minute, so a code is attached only to a
    cancelled sailing, and only when every FOI 4202 row at that minute gives the same code."""
    reasons = reasons or {}
    out = []
    for row in rows:
        d = _as_date(row.get("Scheduled Departure Date"))
        t = _as_time(row.get("Scheduled Departure Time"))
        if d is None or t is None:
            continue
        leg = (row.get("Scheduled Leg") or "").strip()
        route = ARRAN_LEGS.get(leg)
        origin, _, destination = leg.partition("-")
        departure = uk_to_utc(d, t)
        arrival_t = _as_time(row.get("Scheduled Arrival Time"))
        arrival = uk_to_utc(d, arrival_t)
        if arrival and departure and arrival < departure:
            arrival += timedelta(days=1)  # crosses midnight
        actual_d = _as_date(row.get("Actual Departure Date")) or d
        actual_dep = uk_to_utc(actual_d, _as_time(row.get("Actual Departure Time")))
        actual_arr = uk_to_utc(actual_d, _as_time(row.get("Actual Arrival Time")))
        if actual_arr and actual_dep and actual_arr < actual_dep:
            actual_arr += timedelta(days=1)
        label = label_for(row)
        local = f"{t:%H:%M}"
        codes = reasons.get((d, local), set())
        detail = next(iter(codes)) if (label.startswith("cancelled") and len(codes) == 1) else None
        outcome = row.get("Contractual Outcome")
        vessel_raw = row.get("Ship Name") or ""

        exclusion = None
        if outcome == "Not Operated" or "DUMMY" in vessel_raw.upper():
            exclusion = "not_operated_placeholder"
        elif outcome == "Additional":
            exclusion = "additional_unscheduled"
        elif route is None:
            exclusion = "not_an_arran_route_leg"
        elif detail in EXCLUDED_REASONS:
            exclusion = "planned_timetable_reduction"

        out.append(
            {
                "source": SOURCE,
                "sailing_key": sailing_key(route, origin, departure) if route else None,
                "route_code": route,
                "sailing_date": d,
                "departure_local": local,
                "departure_at": departure,
                "arrival_at": arrival,
                "actual_departure_at": actual_dep,
                "actual_arrival_at": actual_arr,
                "scheduled_leg": leg or None,
                "operated_leg": row.get("Operated Leg"),
                "origin": origin or None,
                "destination": destination or None,
                "vessel": normalise_vessel_name(vessel_raw) or None,
                "outcome": outcome,
                "label": label,
                "reason_detail": detail,
                "late_reason": late_reason(row),
                "delay_minutes": ((actual_dep - departure).total_seconds() / 60) if (actual_dep and departure) else None,
                "train": exclusion is None,
                "exclusion": exclusion,
            }
        )
    return out


def read_annex_e(path: Path) -> list[dict]:
    from openpyxl import load_workbook  # optional dependency: pip install -e ".[foi]"

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb["Export"]
    it = ws.iter_rows(values_only=True)
    header = [str(h).strip() if h is not None else "" for h in next(it)]
    return [dict(zip(header, r)) for r in it if any(v is not None for v in r)]


def read_annex_d(path: Path) -> dict[tuple[date, str], set[str]]:
    """Arran rows of the FOI 4202 cancellations CSV: the reason codes given at each (date, HH:MM)."""
    out: dict[tuple[date, str], set[str]] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if "Brodick" not in (r.get("Route") or ""):
                continue
            d, t = _as_date(r.get("Sched Dept Date")), _as_time(r.get("Sched Dept Time"))
            code = (r.get("Dominant Reason") or "").strip()
            if d and t and code:
                out.setdefault((d, f"{t:%H:%M}"), set()).add(code)
    return out


def write_labels(rows: list[dict], out: Path) -> None:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    pq.write_table(pa.Table.from_pylist(rows, schema=LABEL_SCHEMA), tmp)
    tmp.replace(out)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (2, 3):
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    annex_e, out = Path(args[0]), Path(args[-1])
    reasons = read_annex_d(Path(args[1])) if len(args) == 3 else {}
    rows = normalise(read_annex_e(annex_e), reasons)
    write_labels(rows, out)
    counts: dict[str, int] = {}
    for r in rows:
        if r["train"]:
            counts[r["label"]] = counts.get(r["label"], 0) + 1
    print({"rows": len(rows), "train": sum(r["train"] for r in rows), "labels": counts})
    return 0


if __name__ == "__main__":
    sys.exit(main())
