from datetime import datetime, timezone
from pathlib import Path

from ferrypredictor.calmac import Route
from ferrypredictor.status import effective_status, normalise_reason

import json

FIX = Path(__file__).parent / "fixtures" / "calmac"
NOON = datetime(2026, 11, 23, 12, 30, tzinfo=timezone.utc)


def routes():
    data = json.loads((FIX / "routes_2026-11-23.json").read_text(encoding="utf-8"))
    return {r["routeCode"]: Route.model_validate(r) for r in data["data"]["routes"]}


def test_route_milder_than_its_notice_takes_the_notice():
    r = routes()["301"]
    r.status = "BE_AWARE"  # as seen in the morning: route field milder than its SAILING notice
    assert effective_status(r, NOON) == "DISRUPTIONS"


def test_ardrossan_is_be_aware():
    assert effective_status(routes()["003"], NOON) == "BE_AWARE"


def test_expired_sailing_notice_is_ignored():
    r = routes()["301"]
    r.status = "NORMAL"
    after_notice_ends = datetime(2026, 11, 27, tzinfo=timezone.utc)
    assert effective_status(r, after_notice_ends) == "NORMAL"


def test_service_and_information_notices_never_raise_status():
    r = routes()["003"]
    r.status = "NORMAL"
    r.route_statuses = [s for s in r.route_statuses if s.status != "SAILING"]
    assert effective_status(r, NOON) == "NORMAL"


def test_unknown_status_value_ranks_as_be_aware():
    r = routes()["003"]
    r.status = "SOMETHING_NEW"
    r.route_statuses = []
    assert effective_status(r, NOON) == "BE_AWARE"


def test_normalise_reason():
    assert normalise_reason(None) is None
    assert normalise_reason("") is None
    assert normalise_reason("None") is None
    assert normalise_reason("Crew Availability") == "crew availability"
    assert normalise_reason(" Crew availability ") == "crew availability"
    assert normalise_reason("Weather") == "weather"
