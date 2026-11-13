from __future__ import annotations

from cabin.controller import AssistantController


def test_driver_event_opens_and_closes():
    c = AssistantController()
    assert c.driverOpen is False
    seen = []
    c.driverChanged.connect(lambda: seen.append(1))
    c.apply("driver", {"open": True, "tab": "face", "state": {"face": {}}})
    assert c.driverOpen is True
    assert c.driver["tab"] == "face"
    c.apply("driver", {"open": False, "tab": "face", "state": {}})
    assert c.driverOpen is False
    assert len(seen) == 2


def test_map_live_event():
    c = AssistantController()
    assert c.mapLive is False
    c.apply("map_live", True)
    assert c.mapLive is True
    c.apply("map_live", False)
    assert c.mapLive is False
