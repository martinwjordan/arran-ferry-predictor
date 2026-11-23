import copy
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from ferrypredictor import collect, storage
from ferrypredictor.calmac import Route, Sailing

FIX = Path(__file__).parent / "fixtures" / "calmac"
NOW = datetime(2026, 11, 23, 12, 30, tzinfo=timezone.utc)


def load_routes():
    data = json.loads((FIX / "routes_2026-11-23.json").read_text(encoding="utf-8"))
    return [Route.model_validate(r) for r in data["data"]["routes"]]


def load_sailings(code):
    data = json.loads((FIX / f"sailings_{code}_2026-11-23.json").read_text(encoding="utf-8"))
    return [Sailing.model_validate(s) for s in data["data"]["portPairSailings"]]


class FakeClient:
    def __init__(self):
        self.routes = load_routes()
        self.sailings = {"003": load_sailings("003"), "301": load_sailings("301")}

    def fetch_routes(self, codes):
        return [r for r in self.routes if r.route_code in codes]

    def fetch_sailings(self, code, start: date, end: date):
        return [s for s in self.sailings[code] if start <= s.sailing_date < end]


@pytest.fixture
def dirs(tmp_path):
    return tmp_path / "data", tmp_path / "site"


def run(client, dirs, now=NOW):
    data_dir, site_dir = dirs
    return collect.run_collect(client, now, data_dir, site_dir)


def notices(dirs):
    return storage.read_dataset(dirs[0] / "notices", collect.NOTICE_SCHEMA).to_pylist()


def sailing_rows(dirs):
    return storage.read_dataset(dirs[0] / "sailings", collect.SAILING_SCHEMA).to_pylist()


def test_first_run_writes_everything(dirs):
    client = FakeClient()
    summary = run(client, dirs)
    n_sailings = len(client.sailings["003"]) + len(client.sailings["301"])
    assert summary == {"notices": 13, "sailings": n_sailings, "route_status": 2}
    assert {r["change_type"] for r in sailing_rows(dirs)} == {"new"}
    weather = [n for n in notices(dirs) if n["notice_type"] == "SAILING" and n["route_code"] == "301"]
    assert weather[0]["reason"] == "weather"
    assert "18:10 departing Troon" in weather[0]["detail"]
    health = json.loads((dirs[1] / "health.json").read_text())
    assert health["jobs"]["collect"]["details"]["notices"] == 13


def test_identical_second_run_writes_nothing(dirs):
    client = FakeClient()
    run(client, dirs)
    assert run(client, dirs) == {"notices": 0, "sailings": 0, "route_status": 0}


def test_updated_notice_writes_one_new_version(dirs):
    client = FakeClient()
    run(client, dirs)
    notice = next(n for n in client.routes[1].route_statuses if n.status == "SAILING")
    notice.updated_at_date_time = datetime(2026, 11, 23, 13, 0, tzinfo=timezone.utc)
    notice.detail = notice.detail + "\r\n\r\nThere will be a review at 15:00."
    assert run(client, dirs)["notices"] == 1
    versions = [n for n in notices(dirs) if n["notice_id"] == notice.id]
    assert len(versions) == 2


def test_future_sailing_vanishing_is_recorded_as_removed(dirs):
    client = FakeClient()
    run(client, dirs)
    s1810 = next(s for s in client.sailings["301"] if s.departure_date_time == datetime(2026, 11, 23, 18, 10, tzinfo=timezone.utc))
    client.sailings["301"].remove(s1810)
    assert run(client, dirs)["sailings"] == 1
    removed = [r for r in sailing_rows(dirs) if r["change_type"] == "removed"]
    assert [r["sailing_id"] for r in removed] == [s1810.id]


def test_departed_sailing_vanishing_writes_nothing(dirs):
    client = FakeClient()
    run(client, dirs)
    s0730 = next(s for s in client.sailings["301"] if s.departure_date_time == datetime(2026, 11, 23, 7, 30, tzinfo=timezone.utc))
    client.sailings["301"].remove(s0730)
    assert run(client, dirs)["sailings"] == 0


def test_rescheduled_sailing_is_recorded_as_changed(dirs):
    client = FakeClient()
    run(client, dirs)
    s = copy.deepcopy(client.sailings["003"][-1])
    s.rescheduled_departure_date_time = datetime(2026, 11, 29, 19, 45, tzinfo=timezone.utc)
    client.sailings["003"][-1] = s
    assert run(client, dirs)["sailings"] == 1
    changed = [r for r in sailing_rows(dirs) if r["change_type"] == "changed"]
    assert changed[0]["sailing_id"] == s.id


def test_route_status_change_writes_a_row(dirs):
    client = FakeClient()
    run(client, dirs)
    client.routes[0].status = "DISRUPTIONS"
    assert run(client, dirs)["route_status"] == 1
