import asyncio

from cabin import thermal
from cabin.thermal import ThermalLadder


def _ladder(temps, **kw):
    it = iter(temps)
    state = {"t": 0.0}
    calls = []

    def read_temp():
        state["t"] = next(it)
        return state["t"]

    ladder = ThermalLadder(read_temp, lambda: 0, lambda n, on: calls.append((n, on)), **kw)
    return ladder, calls


def test_thermal_ladder_order():
    ladder, calls = _ladder([70, 78, 79, 81, 84, 90, 82, 80, 78.5, 76, 74, 70])
    for _ in range(12):
        ladder.tick()
    assert calls == [
        ("avatar_30fps", True), ("short_tts", True), ("map_service_off", True),
        ("map_service_off", False), ("short_tts", False), ("avatar_30fps", False),
    ]


def test_hysteresis_holds_inside_band():
    ladder, calls = _ladder([79, 76, 75.5, 74.9])
    for _ in range(4):
        ladder.tick()
    assert calls == [("avatar_30fps", True), ("avatar_30fps", False)]
    assert ladder.level == 0


def test_jump_applies_all_levels_in_order():
    ladder, calls = _ladder([85])
    ladder.tick()
    assert [c[0] for c in calls] == ["avatar_30fps", "short_tts", "map_service_off"]
    assert ladder.active == ["avatar_30fps", "short_tts", "map_service_off"]


def test_emit_payload():
    seen = []
    ladder, _ = _ladder([79], emit=lambda k, v: seen.append((k, v)))
    ladder.read_throttled = lambda: 0x50005
    ladder.tick()
    key, payload = seen[-1]
    assert key == "thermal"
    assert payload == {"level": 1, "temp_c": 79, "throttled": "0x50005", "active": ["avatar_30fps"]}


class _Eng:
    def __init__(self, cfg=None):
        self.events = []
        self.emit = lambda k, v: self.events.append((k, v))
        self.cfg = cfg


class _Cfg:
    def __init__(self, **kv):
        self.kv = kv

    def get(self, key, default=""):
        return self.kv.get(key, default)


def test_install_sets_flags_and_closer(monkeypatch):
    temps = iter([85, 70, 70, 70, 70])
    monkeypatch.setattr(thermal, "_make_readers", lambda: (lambda: next(temps, 70), lambda: "0x0", None))
    eng = _Eng(_Cfg(THERMAL_INTERVAL_S="0.01", THERMAL_HYSTERESIS_C="3"))
    eng.closers = []

    async def go():
        thermal.install(eng)
        assert eng.thermal_flags == {"map_service_off": False, "avatar_30fps": False, "short_tts": False}
        assert eng.short_tts_max_words == 12
        await asyncio.sleep(0.05)
        assert any(v["level"] == 3 for k, v in eng.events if k == "thermal")
        assert eng.thermal_flags == {"map_service_off": False, "avatar_30fps": False, "short_tts": False}
        await eng.closers[0]()

    asyncio.run(go())


def test_install_off_and_no_sensor(monkeypatch):
    eng = _Eng(_Cfg(THERMAL="off"))
    thermal.install(eng)
    assert eng.thermal_flags == {"map_service_off": False, "avatar_30fps": False, "short_tts": False}
    assert eng.short_tts_max_words == 12
    assert not hasattr(eng, "closers") and eng.events == []

    monkeypatch.setattr(thermal, "_make_readers", lambda: None)
    eng = _Eng(_Cfg())

    async def go():
        thermal.install(eng)

    asyncio.run(go())
    assert not hasattr(eng, "closers")


def test_parse_helpers():
    assert thermal.parse_vcgencmd_temp("temp=61.8'C\n") == 61.8
    assert thermal.parse_throttled("throttled=0x50005\n") == "0x50005"
    assert thermal.parse_levels("78,81,84") == (78.0, 81.0, 84.0)
    assert thermal.parse_levels("bad") == (78.0, 81.0, 84.0)


def test_exact_thresholds():
    ladder, _calls = _ladder([77.99, 78.0, 75.0, 74.99])
    levels = [ladder.tick() for _ in range(4)]
    assert levels == [0, 1, 1, 0]


def test_hold_in_band_at_level_3():
    ladder, _ = _ladder([84, 81.5, 81.0, 81.1])
    assert [ladder.tick() for _ in range(4)] == [3, 3, 3, 3]


def test_failing_throttled_still_sheds():
    def bad():
        raise RuntimeError("hung")

    calls = []
    seen = []
    ladder = ThermalLadder(lambda: 85, bad, lambda n, on: calls.append((n, on)),
                           emit=lambda k, v: seen.append(v))
    assert ladder.tick() == 3
    assert seen[-1]["throttled"] == "0x0"
    asyncio.run(ladder.step())
    assert ladder.level == 3


def test_sysfs_fallback_after_vcgencmd_failure():
    primary = {"n": 0}

    def bad():
        primary["n"] += 1
        raise RuntimeError("vcgencmd gone")

    ladder = ThermalLadder(bad, lambda: "0x0", lambda n, on: None, fallback_temp=lambda: 82.0)
    assert ladder.tick() == 2
    ladder.tick()
    ladder.tick()
    assert primary["n"] == 3
    ladder.tick()
    assert primary["n"] == 3
