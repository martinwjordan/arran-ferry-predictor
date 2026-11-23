"""Client for CalMac's public website feed (GraphQL behind Azure API Management; no key).

Unofficial: the schema can change without notice and introspection is switched off,
so every response is validated and GraphQL error messages are kept in full.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime

import httpx
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from . import __version__

API_URL = "https://apim.calmac.co.uk/graphql"
REPO_URL = "https://github.com/martinwjordan/arran-ferry-predictor"


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class Vessel(_Model):
    name: str
    vessel_code: str | None = None
    is_active: bool | None = None


class RouteStatus(_Model):
    """A notice on a route. `status` is the notice type: SAILING, SERVICE, INFORMATION or WARNING."""

    id: str
    title: str
    status: str
    sub_status: str | None = None
    detail: str | None = None
    disruption_reason: str | None = None
    start_date_time: datetime | None = None
    end_date_time: datetime | None = None
    next_review_date_time: datetime | None = None
    updated_at_date_time: datetime
    is_active: bool | None = None


class Route(_Model):
    route_code: str
    text_code: str | None = None
    name: str
    status: str
    vessels: list[Vessel] = []
    route_statuses: list[RouteStatus] = []


class Port(_Model):
    name: str
    port_code: str | None = None


class Sailing(_Model):
    id: int
    sailing_date: date
    status: str
    departure_date_time: datetime
    arrival_date_time: datetime | None = None
    rescheduled_departure_date_time: datetime | None = None
    rescheduled_arrival_date_time: datetime | None = None
    origin_port: Port
    destination_port: Port


class CalMacError(RuntimeError):
    pass


_ROUTES_QUERY = """query {{ routes (where: {{ routeCode: {{ in: {codes} }} }}) {{ routeCode textCode name status
  vessels {{ name vesselCode isActive }}
  routeStatuses {{ id title status subStatus detail disruptionReason startDateTime endDateTime nextReviewDateTime updatedAtDateTime isActive }} }} }}"""

_SAILINGS_QUERY = """query ($r: String, $s: Date, $e: Date) {
  portPairSailings (where: { and: [ { route: { routeCode: { eq: $r } } } { sailingDate: { gte: $s } } { sailingDate: { lt: $e } } ] } order: { departureDateTime: ASC }) {
    id sailingDate status departureDateTime arrivalDateTime rescheduledDepartureDateTime rescheduledArrivalDateTime
    originPort { name portCode } destinationPort { name portCode } } }"""


class CalMacClient:
    def __init__(
        self,
        contact_email: str,
        base_url: str = API_URL,
        timeout: float = 20.0,
        retry_wait: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url
        self.retry_wait = retry_wait
        self._http = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"arran-ferry-predictor/{__version__} (+{REPO_URL}; {contact_email})",
            },
        )

    def query(self, gql: str, variables: dict | None = None) -> dict:
        payload = {"query": gql, "variables": variables or {}}
        for attempt in (1, 2):
            try:
                r = self._http.post(self.base_url, json=payload)
            except httpx.TransportError as e:
                if attempt == 2:
                    raise CalMacError(f"network error: {e}") from e
                time.sleep(self.retry_wait)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 2:
                    raise CalMacError(f"HTTP {r.status_code} from CalMac")
                time.sleep(self.retry_wait)
                continue
            try:
                body = r.json()
            except ValueError:
                body = None
            errors = (body or {}).get("errors")
            if r.status_code >= 400 or errors:
                msg = errors[0].get("message", "GraphQL error") if errors else r.text[:300]
                raise CalMacError(f"HTTP {r.status_code}: {msg}")
            if not body or "data" not in body:
                raise CalMacError("response has no data")
            return body["data"]
        raise CalMacError("unreachable")

    def fetch_routes(self, codes: list[str]) -> list[Route]:
        data = self.query(_ROUTES_QUERY.format(codes=json.dumps(list(codes))))
        return [Route.model_validate(r) for r in data["routes"]]

    def fetch_sailings(self, route_code: str, start: date, end: date) -> list[Sailing]:
        """Sailings with sailingDate from `start` (inclusive) to `end` (exclusive)."""
        data = self.query(_SAILINGS_QUERY, {"r": route_code, "s": start.isoformat(), "e": end.isoformat()})
        return [Sailing.model_validate(s) for s in data["portPairSailings"]]
