"""Turn-by-turn cues, a single reroute, and the post-checkout "Drive there?" ask."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from driveos.domain.models import GeoPoint, Maneuver, Route, RouteStep, VoiceCue
from driveos.nav.geo import point_at, with_cumulative

from cabin.nav.agent import NavAgent
from cabin.payment import Payment

M_PER_DEG = 111_132.0


def route_with_cue() -> Route:
    start = GeoPoint(lat=10.0, lon=78.0)
    turn = GeoPoint(lat=10.0 + 500 / M_PER_DEG, lon=78.0)
    end = GeoPoint(lat=turn.lat, lon=78.0 + 400 / M_PER_DEG)
    steps = [
        RouteStep(name="Main", distance_m=500, duration_s=60,
                  maneuver=Maneuver(type="depart", modifier="straight", instruction="Head north", location=start),
                  geometry_index=0,
                  voice=[VoiceCue(at_m=200, text="In 200 metres, turn right")]),
        RouteStep(name="Cross", distance_m=400, duration_s=40,
                  maneuver=Maneuver(type="turn", modifier="right", instruction="Turn right", location=turn),
                  geometry_index=1, voice=[]),
        RouteStep(name="", distance_m=0, duration_s=0,
                  maneuver=Maneuver(type="arrive", modifier=None, instruction="Arrive", location=end),
                  geometry_index=2, voice=[]),
    ]
    return with_cumulative(Route(geometry=[start, turn, end], distance_m=900, duration_s=100, steps=steps,
                                 destination_name="Anna Cafe"))


class Mapbox:
    def __init__(self, route):
        self.route = route
        self.calls = 0

    async def directions(self, points, names=None):
        self.calls += 1
        return self.route


class Engine:
    def __init__(self):
        self.vehicle = SimpleNamespace(lat=10.0, lon=78.0, pos=(10.0, 78.0))
        self.events = []
        self.cues = []
        self.state = "idle"

    def emit(self, key, value):
        self.events.append((key, value))

    async def say_cue(self, text):
        self.cues.append(text)


def run(coro):
    return asyncio.run(coro)


def test_cue_spoken_at_distance():
    road = route_with_cue()
    eng, box = Engine(), Mapbox(road)
    agent = NavAgent(eng, box, clock=lambda: 0.0)
    run(agent.start(SimpleNamespace(id="anna", name="Anna Cafe", lat=10.01, lon=78.01, address="", category="coffee")))
    here, _heading, _seg = point_at(road, 310)
    run(agent.tick(here))
    run(agent.tick(here))
    assert eng.cues == ["In 200 metres, turn right"]
    nav = [v for k, v in eng.events if k == "nav"]
    assert nav and nav[-1]["next_maneuver"]


def test_offroute_triggers_single_reroute():
    road = route_with_cue()
    eng, box = Engine(), Mapbox(road)
    agent = NavAgent(eng, box, clock=lambda: 1000.0)
    run(agent.start(SimpleNamespace(id="anna", name="Anna Cafe", lat=10.01, lon=78.01, address="", category="")))
    off = GeoPoint(lat=10.0, lon=78.0 + 60 / (111_320.0 * 0.9848))
    for _ in range(3):
        run(agent.tick(off))
    assert box.calls == 2


class _PayEngine:
    def __init__(self):
        from cabin.config import Settings
        self.cfg = Settings({"PAY_SIM_STEP_S": "0.01"})
        self.sim = True
        self.state = "idle"
        self.events = []
        self.spoken = []
        self.action_handlers, self.intercepts, self.system_extra = {}, [], []
        self.tools = SimpleNamespace(extend=lambda *a, **k: None)
        self.vehicle = SimpleNamespace(lat=10.79, lon=78.70)
        self.last_card = None
        self.started = []

        class Agent:
            async def start(inner, dest):
                self.started.append(dest)

        self.nav = SimpleNamespace(agent=Agent())
        self.shop = SimpleNamespace(
            cart_place=SimpleNamespace(id="poi-anna", name="Anna Cafe", lat=10.81, lon=78.72, address="Trichy"),
            place=None)

    def emit(self, key, value):
        self.events.append((key, value))

    def _set_state(self, s, caption=None):
        self.state = s

    async def _speak_loop(self, q):
        while (item := await q.get()) is not None:
            self.spoken.append(item)


def test_drive_there_after_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVA_CABIN_DATA", str(tmp_path))
    eng = _PayEngine()
    pay = Payment(eng)
    pay.install()
    cart = {"lines": [{"item_id": "i1", "name": "Filter coffee", "qty": 1, "unit_price": 40.0, "options": {}}],
            "total": 40.0, "currency": "INR",
            "place": {"id": "poi-anna", "name": "Anna Cafe", "lat": 10.81, "lon": 78.72, "address": "Trichy"}}
    run(pay.begin(cart))
    result = SimpleNamespace(success=True, tier=SimpleNamespace(value="voice"), method="voice")
    run(pay._capture(pay.co, result))
    asks = [v for k, v in eng.events if k == "ask"]
    assert asks and "Drive there?" in asks[-1]["text"]
    assert run(pay.intercept("yes")) is True
    assert eng.started and eng.started[0].name == "Anna Cafe"
    assert eng.started[0].lat == 10.81


def _drive_engine(tmp_path, monkeypatch, fail=False):
    monkeypatch.setenv("NOVA_CABIN_DATA", str(tmp_path))
    eng = _PayEngine()
    if fail:
        async def boom(dest):
            raise RuntimeError("down")
        eng.nav.agent.start = boom
    pay = Payment(eng)
    pay.install()
    pay._drive = {"id": "p", "name": "Anna Cafe", "lat": 10.81, "lon": 78.72}
    pay._drive_at = __import__("time").monotonic()
    eng.say_calls = []

    async def say(text):
        eng.say_calls.append(text)
    pay.say = say
    return eng, pay


def test_drive_ask_expires_after_30_seconds(tmp_path, monkeypatch):
    eng, pay = _drive_engine(tmp_path, monkeypatch)
    pay._drive_at -= 31
    assert run(pay.intercept("yes")) is False
    assert pay._drive is None and not eng.started


def test_drive_ask_cleared_by_unrelated_turn(tmp_path, monkeypatch):
    eng, pay = _drive_engine(tmp_path, monkeypatch)
    assert run(pay.intercept("what's the weather")) is False
    assert pay._drive is None
    assert run(pay.intercept("yes")) is False and not eng.started


def test_drive_start_failure_is_spoken(tmp_path, monkeypatch):
    eng, pay = _drive_engine(tmp_path, monkeypatch, fail=True)
    assert run(pay.intercept("yes")) is True
    assert eng.say_calls == ["I couldn't start navigation."]


class _CueEngine:
    def __init__(self):
        from cabin.config import Settings
        self.cfg = Settings({})
        self.sim = False
        self.state = "idle"
        self.events = []
        self.vehicle = SimpleNamespace(lat=10.0, lon=78.0, pos=(10.0, 78.0))
        self.spoken = []
        self.tools = SimpleNamespace()
        self.closers = []
        self.hold = None

    def emit(self, k, v):
        self.events.append((k, v))

    def _set_state(self, s, caption=None):
        self.state = s
        self.emit("state", s)

    async def _speak_loop(self, q):
        self.state = "speaking"
        while (item := await q.get()) is not None:
            self.spoken.append(item)
        if self.hold is not None:
            await self.hold.wait()


def _plugin_engine(monkeypatch):
    from cabin.nav import plugin
    eng = _CueEngine()
    monkeypatch.setattr(plugin, "GpsFeed", lambda *a, **k: SimpleNamespace(
        start=lambda: None, stop=lambda: None, spec="x"))
    return plugin, eng


def test_queued_cues_are_spoken_when_engine_returns_to_idle(monkeypatch):
    async def go():
        plugin, eng = _plugin_engine(monkeypatch)
        plugin.install(eng)
        eng.state = "thinking"
        await eng.say_cue("turn left")
        assert eng.spoken == [] and eng._nav_cues == ["turn left"]
        eng.state = "idle"
        await eng._cue_drain.drain_once()
        assert eng.spoken == ["turn left"] and eng._nav_cues == []
        for closer in eng.closers:
            res = closer()
            if hasattr(res, "__await__"):
                await res
    run(go())


def test_cue_does_not_idle_the_engine_after_a_nova_turn_started(monkeypatch):
    async def go():
        plugin, eng = _plugin_engine(monkeypatch)
        plugin.install(eng)
        eng.hold = asyncio.Event()
        task = asyncio.create_task(eng.say_cue("turn right"))
        await asyncio.sleep(0.01)
        eng.state = "thinking"
        eng.emit("state", "thinking")
        eng.state = "speaking"
        eng.hold.set()
        await task
        assert eng.state == "speaking"
    run(go())


def test_one_tick_task_at_a_time_and_failures_are_logged(monkeypatch, caplog):
    from cabin.nav.plugin import _TickRunner

    class Agent:
        def __init__(self):
            self.n = 0
            self.gate = asyncio.Event()

        async def tick(self, fix):
            self.n += 1
            await self.gate.wait()
            raise ValueError("bad fix")

    async def go():
        agent = Agent()
        ticks = _TickRunner(agent)
        ticks.submit((1, 2))
        ticks.submit((1, 3))
        await asyncio.sleep(0.01)
        assert agent.n == 1
        agent.gate.set()
        await asyncio.sleep(0.01)
        ticks.submit((1, 4))
        await asyncio.sleep(0.01)
        assert agent.n == 2
    with caplog.at_level("WARNING", logger="nav.plugin"):
        run(go())
    assert any("guidance tick failed" in r.message for r in caplog.records)
