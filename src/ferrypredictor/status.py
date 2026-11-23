"""Route status and disruption-reason normalisation."""

from __future__ import annotations

from datetime import datetime

from .calmac import Route

ORDER = ["NORMAL", "BE_AWARE", "DISRUPTIONS", "ALL_SAILINGS_CANCELLED"]
_RANK = {s: i for i, s in enumerate(ORDER)}


def _rank(status: str | None) -> int:
    s = (status or "NORMAL").upper()
    if s in ("NONE", ""):
        return 0
    # An unfamiliar value is treated as a warning rather than ignored.
    return _RANK.get(s, _RANK["BE_AWARE"])


def effective_status(route: Route, now: datetime) -> str:
    """Worst of the route's own status and its SAILING notices active at `now`.

    CalMac's route status can be milder than its own notices (seen 23 November 2026).
    """
    worst = _rank(route.status)
    for n in route.route_statuses:
        if n.status.upper() != "SAILING":
            continue
        if n.start_date_time and n.start_date_time > now:
            continue
        if n.end_date_time and n.end_date_time <= now:
            continue
        worst = max(worst, _rank(n.sub_status))
    return ORDER[worst]


def normalise_reason(reason: str | None) -> str | None:
    """Lower case and trimmed; empty and the literal string 'None' become None."""
    r = (reason or "").strip()
    if not r or r.lower() == "none":
        return None
    return r.lower()
