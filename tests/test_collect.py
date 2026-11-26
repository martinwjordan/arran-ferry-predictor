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
    health = json.loads((dirs[1] / "health" / "collect.json").read_text())
    assert health["details"]["notices"] == 13


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
    assert [r["sailing_key"] for r in removed] == ["301|Troon|2026-11-23T18:10:00+00:00"]


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
    assert changed[0]["sailing_key"].startswith("003|")


def test_reissued_calmac_ids_are_not_changes(dirs):
    # Seen live on 23 November 2026: CalMac gave every sailing a new id between 12:36 and 16:02.
    client = FakeClient()
    run(client, dirs)
    for code in ("003", "301"):
        client.sailings[code] = [s.model_copy(update={"id": s.id + 46494}) for s in client.sailings[code]]
    assert run(client, dirs)["sailings"] == 0


def test_rows_carry_a_stable_sailing_key(dirs):
    client = FakeClient()
    run(client, dirs)
    row = next(r for r in sailing_rows(dirs) if r["origin"] == "Troon" and r["departure_at"].hour == 18)
    assert row["sailing_key"] == "301|Troon|2026-11-23T18:10:00+00:00"
    assert row["sailing_id"] is not None


def test_state_keyed_by_calmac_id_is_migrated(dirs):
    import json as _json
    client = FakeClient()
    run(client, dirs)
    state_path = dirs[0] / "state" / "sailings_last.json"
    state = _json.loads(state_path.read_text())
    # Rewrite the state in the old format: keyed by CalMac id, no key field.
    old = {}
    for key, v in state.items():
        old[str(v["calmac_id"])] = {k: x for k, x in v.items() if k not in ("calmac_id",)}
    state_path.write_text(_json.dumps(old))
    assert run(client, dirs)["sailings"] == 0


def test_route_status_change_writes_a_row(dirs):
    client = FakeClient()
    run(client, dirs)
    client.routes[0].status = "DISRUPTIONS"
    assert run(client, dirs)["route_status"] == 1


def test_empty_feed_is_a_gap_not_cancellations(dirs):
    """26 Nov 2026, 14:47: CalMac's API returned no sailings anywhere; nothing must be logged as removed."""
    client = FakeClient()
    run(client, dirs)
    full = copy.deepcopy(client.sailings)
    client.sailings = {"003": [], "301": []}
    later = datetime(2026, 11, 23, 14, 47, tzinfo=timezone.utc)
    assert run(client, dirs, now=later)["sailings"] == 0
    assert {r["change_type"] for r in sailing_rows(dirs)} == {"new"}
    health = json.loads((dirs[1] / "health" / "collect.json").read_text())
    assert health["details"]["sailings_feed_empty"] == ["003", "301"]
    # Last known sailings are carried forward, so the feed coming back logs no changes
    client.sailings = full
    assert run(client, dirs, now=datetime(2026, 11, 23, 15, 7, tzinfo=timezone.utc))["sailings"] == 0


def test_one_route_empty_keeps_the_other_route_live(dirs):
    client = FakeClient()
    run(client, dirs)
    client.sailings["003"] = []
    s1810 = next(s for s in client.sailings["301"] if s.departure_date_time == datetime(2026, 11, 23, 18, 10, tzinfo=timezone.utc))
    client.sailings["301"].remove(s1810)
    assert run(client, dirs)["sailings"] == 1
    removed = [r["sailing_key"] for r in sailing_rows(dirs) if r["change_type"] == "removed"]
    assert removed == ["301|Troon|2026-11-23T18:10:00+00:00"]
