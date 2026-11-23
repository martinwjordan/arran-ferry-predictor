from ferrypredictor import config


def test_load_routes_returns_both_arran_routes():
    routes = config.load_routes()
    assert {r.code for r in routes} == {"003", "301"}
    for r in routes:
        assert len(r.ports) == 2
        assert "Brodick" in r.ports


def test_load_points_returns_five_points_in_the_clyde():
    points = config.load_points()
    assert set(points) == {"ardrossan", "troon", "brodick", "mid_ardrossan", "mid_troon"}
    for p in points.values():
        assert 55.4 < p.lat < 55.8
        assert -5.3 < p.lon < -4.5


def test_load_bbox_is_ordered_south_west_to_north_east():
    box = config.load_bbox()
    assert box.lat_min < box.lat_max
    assert box.lon_min < box.lon_max


def test_load_vessels_normalises_names_and_allows_missing_mmsi():
    vessels = config.load_vessels()
    names = {v.name for v in vessels}
    assert "CALEDONIAN ISLES" in names
    assert "ALFRED" in names
    assert all(v.mmsi is None or isinstance(v.mmsi, int) for v in vessels)


def test_normalise_vessel_name():
    assert config.normalise_vessel_name("MV ALFRED  ") == "ALFRED"
    assert config.normalise_vessel_name("mv Caledonian Isles") == "CALEDONIAN ISLES"
    assert config.normalise_vessel_name("  glen  rosa ") == "GLEN ROSA"
    assert config.normalise_vessel_name(None) == ""
