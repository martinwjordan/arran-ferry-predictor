import json
from datetime import date, timezone
from pathlib import Path

import httpx
import pytest
import respx

from ferrypredictor.calmac import API_URL, CalMacClient, CalMacError

FIX = Path(__file__).parent / "fixtures" / "calmac"


def fixture_text(name):
    return (FIX / name).read_text(encoding="utf-8")


@pytest.fixture
def client():
    return CalMacClient("test@example.com", retry_wait=0)


@respx.mock
def test_fetch_routes_parses_both_routes_and_notices(client):
    respx.post(API_URL).mock(return_value=httpx.Response(200, text=fixture_text("routes_2026-11-23.json")))
    routes = {r.route_code: r for r in client.fetch_routes(["003", "301"])}
    assert set(routes) == {"003", "301"}
    assert len(routes["003"].route_statuses) == 5
    assert len(routes["301"].route_statuses) == 8
    sailing_notice = next(s for s in routes["301"].route_statuses if s.status == "SAILING")
    assert sailing_notice.disruption_reason == "Weather"
    assert "18:10 departing Troon" in sailing_notice.detail
    assert sailing_notice.updated_at_date_time.tzinfo is not None
    assert [v.name for v in routes["003"].vessels] == ["MV Caledonian Isles"]


@respx.mock
def test_fetch_sailings_parses_rows(client):
    respx.post(API_URL).mock(return_value=httpx.Response(200, text=fixture_text("sailings_301_2026-11-23.json")))
    sailings = client.fetch_sailings("301", date(2026, 11, 23), date(2026, 11, 30))
    today = [s for s in sailings if s.sailing_date == date(2026, 11, 23)]
    assert len(today) == 6
    assert {s.status for s in sailings} == {"SCHEDULED"}
    first = sailings[0]
    assert first.origin_port.name == "Brodick"
    assert first.departure_date_time.utcoffset() == timezone.utc.utcoffset(None)


@respx.mock
def test_sailings_request_sends_route_and_dates(client):
    route = respx.post(API_URL).mock(return_value=httpx.Response(200, text=fixture_text("sailings_003_2026-11-23.json")))
    client.fetch_sailings("003", date(2026, 11, 23), date(2026, 11, 30))
    body = json.loads(route.calls[0].request.content)
    assert body["variables"] == {"r": "003", "s": "2026-11-23", "e": "2026-11-30"}


@respx.mock
def test_graphql_error_message_is_kept(client):
    respx.post(API_URL).mock(
        return_value=httpx.Response(400, json={"errors": [{"message": "The field `vessel` does not exist on the type `PortPairSailingReadModel`."}]})
    )
    with pytest.raises(CalMacError, match="does not exist"):
        client.fetch_routes(["003"])


@respx.mock
def test_retries_once_on_server_error_then_succeeds(client):
    route = respx.post(API_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, text=fixture_text("routes_2026-11-23.json"))]
    )
    assert len(client.fetch_routes(["003", "301"])) == 2
    assert route.call_count == 2


@respx.mock
def test_two_server_errors_raise(client):
    route = respx.post(API_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(CalMacError, match="503"):
        client.fetch_routes(["003"])
    assert route.call_count == 2


@respx.mock
def test_network_error_retried_then_raised(client):
    route = respx.post(API_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(CalMacError, match="boom"):
        client.fetch_routes(["003"])
    assert route.call_count == 2


@respx.mock
def test_user_agent_names_project_and_contact(client):
    route = respx.post(API_URL).mock(return_value=httpx.Response(200, text=fixture_text("routes_2026-11-23.json")))
    client.fetch_routes(["003"])
    ua = route.calls[0].request.headers["user-agent"]
    assert "arran-ferry-predictor" in ua
    assert "test@example.com" in ua
