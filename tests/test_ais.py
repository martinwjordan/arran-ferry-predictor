import json
from datetime import datetime, timezone

import pytest

from ferrypredictor import ais, storage

NOW = datetime(2026, 11, 23, 12, 30, tzinfo=timezone.utc)
CALE = 232003244  # synthetic MMSI for tests
ALFR = 235000001


def position(mmsi, name, lat=55.61, lon=-4.98, sog=14.2, nav=0, t="2026-11-23 12:31:05.123456789 +0000 UTC"):
    return {
        "MessageType": "PositionReport",
        "MetaData": {"MMSI": mmsi, "MMSI_String": mmsi, "ShipName": name, "latitude": lat, "longitude": lon, "time_utc": t},
        "Message": {
            "PositionReport": {
                "Cog": 245.3,
                "Latitude": lat,
                "Longitude": lon,
                "NavigationalStatus": nav,
                "Sog": sog,
                "TrueHeading": 247,
                "UserID": mmsi,
            }
        },
    }


def static(mmsi, name):
    return {
        "MessageType": "ShipStaticData",
        "MetaData": {"MMSI": mmsi, "ShipName": name, "latitude": 55.64, "longitude": -4.82, "time_utc": "2026-11-23 12:31:00 +0000 UTC"},
        "Message": {"ShipStaticData": {"Name": name, "UserID": mmsi, "Destination": "BRODICK"}},
    }


TRACKED = {"CALEDONIAN ISLES", "ALFRED"}


def test_position_report_parses():
    rec = ais.parse_message(position(CALE, "CALEDONIAN ISLES    "), TRACKED, {}, NOW)
    assert rec["mmsi"] == CALE
    assert rec["name"] == "CALEDONIAN ISLES"
    assert rec["sog"] == pytest.approx(14.2)
    assert rec["lat"] == pytest.approx(55.61)
    assert rec["reported_at"] == datetime(2026, 11, 23, 12, 31, 5, 123456, tzinfo=timezone.utc)
    assert rec["received_at"] == NOW


def test_untracked_vessel_is_ignored():
    assert ais.parse_message(position(999, "SOME TRAWLER"), TRACKED, {}, NOW) is None


def test_known_mmsi_matches_even_without_name():
    rec = ais.parse_message(position(ALFR, ""), TRACKED, {ALFR: "ALFRED"}, NOW)
    assert rec["name"] == "ALFRED"


def test_mv_prefix_and_spaces_are_ignored():
    rec = ais.parse_message(position(ALFR, "MV ALFRED  "), TRACKED, {}, NOW)
    assert rec["name"] == "ALFRED"


def test_static_data_parses_without_position_fields():
    rec = ais.parse_message(static(CALE, "CALEDONIAN ISLES"), TRACKED, {}, NOW)
    assert rec["message_type"] == "ShipStaticData"
    assert rec["sog"] is None


def test_bad_time_falls_back_to_none():
    rec = ais.parse_message(position(CALE, "CALEDONIAN ISLES", t="garbage"), TRACKED, {}, NOW)
    assert rec["reported_at"] is None


def test_discovery_adds_new_and_keeps_configured():
    recs = [{"mmsi": CALE, "name": "CALEDONIAN ISLES"}, {"mmsi": 111, "name": "ALFRED"}]
    state = ais.discover(recs, {}, configured={"ALFRED": ALFR})
    assert state == {"CALEDONIAN ISLES": CALE}


def test_subscription_message_shape():
    sub = ais.subscription("KEY", ais.config.BBox(55.45, -5.25, 55.75, -4.55))
    assert sub["APIKey"] == "KEY"
    assert sub["BoundingBoxes"] == [[[55.45, -5.25], [55.75, -4.55]]]
    assert sub["FilterMessageTypes"] == ["PositionReport", "ShipStaticData"]


def test_run_ais_writes_records_state_and_health(tmp_path):
    data_dir, site_dir = tmp_path / "data", tmp_path / "site"

    async def fake_listen(api_key, bbox, seconds):
        assert api_key == "KEY"
        return [position(CALE, "CALEDONIAN ISLES"), position(999, "OTHER"), position(ALFR, "ALFRED", lat=55.55, lon=-4.68, sog=0.1, nav=5)]

    summary = ais.run_ais("KEY", NOW, data_dir, site_dir, seconds=1, listen_fn=fake_listen)
    assert summary == {"messages": 3, "records": 2, "vessels": {"ALFRED": 1, "CALEDONIAN ISLES": 1}}
    rows = storage.read_dataset(data_dir / "ais", ais.AIS_SCHEMA).to_pylist()
    assert {r["name"] for r in rows} == {"CALEDONIAN ISLES", "ALFRED"}
    state = json.loads((data_dir / "state" / "vessels_mmsi.json").read_text())
    assert state == {"ALFRED": ALFR, "CALEDONIAN ISLES": CALE}
    health = json.loads((site_dir / "health.json").read_text())
    assert health["jobs"]["ais"]["details"]["records"] == 2


def test_run_ais_raises_on_stream_error(tmp_path):
    async def failing_listen(api_key, bbox, seconds):
        raise ais.AisError("Api Key Is Not Valid")

    with pytest.raises(ais.AisError):
        ais.run_ais("KEY", NOW, tmp_path / "data", tmp_path / "site", seconds=1, listen_fn=failing_listen)
