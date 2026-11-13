"""Starts the GPS feed and, when a Mapbox token is set, the turn-by-turn agent."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from .gps import GpsFeed
from .live_map import LiveMap

log = logging.getLogger("nav.plugin")


def install(engine) -> None:
    spec = engine.cfg.get("GPS_SOURCE", "")
    if engine.sim and not spec:
        spec = "sim"
    agent = _agent(engine)
    live = LiveMap(engine)
    engine.live_map = live
    original = engine.emit
    cue = _CueState()
    ticks = _TickRunner(agent)

    def emit(key, value):
        original(key, value)
        if key == "state":
            cue.observe(value)
        if key == "gps":
            live.on_gps(value)
        elif key == "nav":
            live.on_nav(value)
        elif key == "card":
            live.on_card(value)
        if key != "gps" or agent is None or not agent.active:
            return
        ticks.submit(value)

    engine.emit = emit

    async def nav_end(_payload):
        if agent is not None:
            await agent.stop()
        live.clear()

    handlers = getattr(engine, "action_handlers", None)
    if handlers is not None:
        handlers["nav_end"] = nav_end
    feed = GpsFeed(engine.cfg, engine.vehicle, engine.emit, spec=spec or "auto")
    engine.gps = feed
    engine.nav = SimpleNamespace(gps=feed, agent=agent)
    tools = getattr(engine, "tools", None)
    if tools is not None:
        tools.nav_agent = agent
    if not callable(getattr(engine, "say_cue", None)):
        engine.say_cue = _say_cue(engine, cue)
        engine._cue_drain = _CueDrain(engine, engine.say_cue)
        engine.say_cue.drain = engine._cue_drain
    feed.start()
    closers = getattr(engine, "closers", None)
    if closers is not None:
        closers.append(feed.stop)
        closers.append(ticks.close)
        drain = getattr(engine, "_cue_drain", None)
        if drain is not None:
            closers.append(drain.close)
        if agent is not None:
            closers.append(agent.close)
        closers.append(live.close)
    log.info("GPS feed: %s", feed.spec)


class _TickRunner:
    """Runs at most one guidance tick at a time and logs a tick that fails."""

    def __init__(self, agent):
        self.agent = agent
        self.task: asyncio.Task | None = None

    def submit(self, fix) -> None:
        if self.agent is None or (self.task is not None and not self.task.done()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.task = loop.create_task(self.agent.tick(fix))
        self.task.add_done_callback(self._done)

    @staticmethod
    def _done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.warning("guidance tick failed: %s", type(exc).__name__, exc_info=exc)

    async def close(self) -> None:
        task = self.task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class _CueState:
    """Tracks whether a Nova turn took over the engine while a cue was being spoken."""

    def __init__(self):
        self.active = False
        self.taken_over = False

    def begin(self) -> None:
        self.active = True
        self.taken_over = False

    def end(self) -> None:
        self.active = False

    def observe(self, state) -> None:
        if self.active and state not in ("speaking", "idle"):
            self.taken_over = True


class _CueDrain:
    """Speaks cues queued while the engine was busy, once it is idle again."""

    def __init__(self, engine, say, period_s: float = 0.4):
        self.engine = engine
        self.say = say
        self.period_s = period_s
        self.task: asyncio.Task | None = None
        self.ensure()

    def ensure(self) -> None:
        if self.task is not None and not self.task.done():
            return
        try:
            self.task = asyncio.get_running_loop().create_task(self._run())
        except RuntimeError:
            self.task = None
            return
        self.task.add_done_callback(_TickRunner._done)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.period_s)
            await self.drain_once()

    async def drain_once(self) -> bool:
        queued = getattr(self.engine, "_nav_cues", None)
        if not queued or getattr(self.engine, "state", "idle") != "idle":
            return False
        text = queued.pop(0)
        await self.say(text, queued_replay=True)
        return True

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


def _agent(engine):
    cfg = getattr(engine, "cfg", None)
    if cfg is None or not cfg.flag("NAV_GUIDANCE", True):
        return None
    token = cfg.get("MAPBOX_ACCESS_TOKEN")
    if not token:
        return None
    try:
        from driveos.config import MapboxConfig
        from driveos.core.http import Http
        from driveos.nav.mapbox import MapboxClient

        from .agent import NavAgent
        client = MapboxClient(MapboxConfig(token=token), Http(timeout=8))
    except (ImportError, OSError, ValueError, TypeError):
        log.warning("navigation agent unavailable")
        return None
    return NavAgent(engine, client)


def _say_cue(engine, cue: _CueState | None = None):
    cue = cue or _CueState()

    async def say_cue(text: str, queued_replay: bool = False) -> None:
        if not queued_replay:
            engine.emit("nav_cue", {"text": text})
        if getattr(engine, "state", "idle") != "idle":
            queued = getattr(engine, "_nav_cues", None)
            if queued is None:
                engine._nav_cues = queued = []
            queued.append(text)
            drain = getattr(say_cue, "drain", None)
            if drain is not None:
                drain.ensure()
            return
        speak = getattr(engine, "_speak_loop", None)
        if speak is None:
            return
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(text)
        queue.put_nowait(None)
        cue.begin()
        try:
            await speak(queue)
        finally:
            cue.end()
            if (not cue.taken_over and getattr(engine, "state", "") == "speaking"
                    and hasattr(engine, "_set_state")):
                engine._set_state("idle", "")

    return say_cue
