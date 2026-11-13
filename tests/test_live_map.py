"""The live map stays optional: no MapLibre library, the panel still opens."""
from types import SimpleNamespace

from cabin.nav.live_map import LiveMap, frame_bounds, pinned_position, pins_geojson, route_geojson


def _engine():
    events = []
    eng = SimpleNamespace(
        emit=lambda k, v: events.append((k, v)),
        vehicle=SimpleNamespace(lat=37.7, lon=-122.4, heading=12.0),
        thermal_flags={}, cfg=SimpleNamespace(get=lambda *_a, **_k: ""),
        nav=SimpleNamespace(agent=None), nav_open=False)
    eng.events = events
    return eng


def test_missing_library_still_opens_the_panel(monkeypatch):
    monkeypatch.setattr("cabin.nav.live_map.load_library", lambda: None)
    eng = _engine()
    live = LiveMap(eng)
    assert live.available is False
    assert live.show() is False
    assert ("map_open", True) in eng.events
    assert ("map_live", True) not in eng.events
    names = [v["name"] for k, v in eng.events if k == "nav"]
    assert names == ["Current location"]
    live.hide()
    assert ("map_open", False) in eng.events


def test_a_hot_pi_keeps_the_map_and_slows_it(monkeypatch):
    monkeypatch.setattr("cabin.nav.live_map.load_library", lambda: None)
    eng = _engine()
    live = LiveMap(eng)
    periods = []
    live._lib = SimpleNamespace(nova_map_set_period=lambda _m, ms: periods.append(ms))
    live._shown = True
    live._map = object()
    eng.thermal_flags["map_service_off"] = True
    assert live._rendering() is True
    live._apply_pace()
    eng.thermal_flags["map_service_off"] = False
    live._apply_pace()
    assert periods == [250, 33]


def test_a_stopped_fix_stays_pinned_inside_satellite_wander():
    held = (37.4419, -122.1430)
    nudged = pinned_position(held, 37.44195, -122.14304, 0.0)
    assert nudged == held
    moved = pinned_position(held, 37.4435, -122.1430, 0.0)
    assert moved != held
    driving = pinned_position(held, 37.44195, -122.14304, 8.0)
    assert driving == (37.44195, -122.14304)


def test_route_line_is_lon_lat_and_thinned():
    assert route_geojson([]) == '{"type":"FeatureCollection","features":[]}'
    line = route_geojson([SimpleNamespace(lat=1.5, lon=2.25), (3.0, 4.5)])
    assert '"c":"u"' in line and "[2.250000,1.500000]" in line and "[4.500000,3.000000]" in line
    long = route_geojson([(i / 100, i / 50) for i in range(1000)], limit=10)
    assert long.count("[") <= 16


def test_a_place_card_previews_without_opening_the_nav_page(monkeypatch):
    monkeypatch.setattr("cabin.nav.live_map.load_library", lambda: None)
    eng = _engine()
    live = LiveMap(eng)
    live.on_card({"hero": {"kind": "map", "pins": [[37.78, -122.41, "dest"], [37.77, -122.42, "car"]]}})
    assert live._preview is True
    assert ("map_open", True) not in eng.events
    assert ("map_live", True) not in eng.events
    live.on_card({"hero": {"kind": "image", "source": "photo"}})
    assert live._preview is False
    assert live._pins == []


def test_agreeing_to_navigate_opens_the_page_on_that_route(monkeypatch):
    monkeypatch.setattr("cabin.nav.live_map.load_library", lambda: None)
    eng = _engine()
    live = LiveMap(eng)
    live.on_card({"hero": {"kind": "map", "pins": [[37.8, -122.3, "dest"]],
                          "route": [[37.7, -122.4], [37.8, -122.3]]}})
    live.on_nav({"map": "still.png", "name": "Cafe",
                 "route": [[37.7, -122.4], [37.8, -122.3]],
                 "pins": [[37.8, -122.3, "dest"]]})
    assert ("map_open", True) in eng.events
    assert live._preview is False
    assert live._line()[0] == [37.7, -122.4]
    live.hide()
    assert ("map_open", False) in eng.events
    assert live._preview is True


def test_pins_skip_the_car_and_a_single_spot_has_no_box():
    pins = pins_geojson([(37.7, -122.4, "car"), (37.8, -122.3, "dest"), (37.6, -122.5, "shop")])
    assert '"k":"dest"' in pins and '"k":"poi"' in pins and "car" not in pins
    assert frame_bounds([(37.7, -122.4)]) is None
    south, west, north, east = frame_bounds([(37.7, -122.4), (37.8, -122.3)])
    assert south < 37.7 and north > 37.8 and west < -122.4 and east > -122.3
