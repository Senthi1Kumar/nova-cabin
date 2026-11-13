"""Live position for the car: a real receiver when there is a fix, the simulator otherwise.

``GpsFeed`` wraps ``driveos.nav.gps.open_source`` and writes the fix into the shared ``Vehicle``.
``GPS_SOURCE`` is ``auto`` (gpsd, then the serial receiver, then the simulator), ``gpsd``,
``serial:/dev/ttyUSB0@9600``, ``replay:<file>`` or ``sim``. Without a fix for ``GPS_FIX_TIMEOUT_S`` the
vehicle returns to its simulated home position and the ``gps`` event carries a visible badge; the real
sources keep being retried.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable

from driveos.config import LocationConfig
from driveos.domain.models import GeoPoint
from driveos.nav.geo import haversine
from driveos.nav.gps import GearInference, GpsFix, bearing, open_source

log = logging.getLogger("nav.gps")

BADGES = {"fix": "Live GPS", "no_fix": "Searching for GPS", "sim": "Simulated position", "last": "Last known position"}
EMIT_INTERVAL_S = 0.5
WATCH_TICK_S = 0.25
MOVE_EPS_DEG = 1e-6
HEADING_EPS_DEG = 1.0
MIN_HEADING_SPEED_MPS = 1.0
MIN_HEADING_MOVE_M = 5.0
RETRY_PAUSE_S = 5.0
SAVE_INTERVAL_S = 30.0


def _last_fix_path():
    from cabin.wallet import data_dir
    return data_dir() / "last_fix.json"


def _load_last_fix(path) -> tuple[float, float] | None:
    """The last real position this device had, so a cold start without sky view stays in town."""
    try:
        d = json.loads(path.read_text())
        lat, lon = float(d["lat"]), float(d["lon"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def _save_last_fix(path, lat: float, lon: float) -> None:
    try:
        path.write_text(json.dumps({"lat": round(lat, 6), "lon": round(lon, 6)}))
    except OSError:
        log.debug("last fix not saved")


class GpsFeed:
    def __init__(self, cfg, vehicle, emit: Callable[[str, object], None], *, spec: str | None = None,
                 opener=open_source, clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep) -> None:
        self.vehicle = vehicle
        self.emit = emit
        self.spec = (spec if spec is not None else cfg.get("GPS_SOURCE", "auto")).strip() or "auto"
        self.timeout_s = cfg.num("GPS_FIX_TIMEOUT_S", 30.0)
        self.max_accuracy_m = cfg.num("GPS_MAX_ACCURACY_M", LocationConfig().max_accuracy_m)
        device = cfg.get("GPS_SERIAL_DEVICE", "/dev/ttyUSB0")
        baud = int(cfg.num("GPS_BAUD", 9600))
        self._candidates = (["gpsd", f"serial:{device}@{baud}"] if self.spec.lower() == "auto"
                            else [self.spec])
        self._open, self._clock, self._sleep = opener, clock, sleep
        self._home = (vehicle.lat, vehicle.lon)
        self._home_place = getattr(vehicle, "place", "")
        self._fallback = "sim"
        self._last_saved = -1e9
        self._store = _last_fix_path()
        saved = _load_last_fix(self._store) if self.spec.lower() != "sim" else None
        if saved is not None:
            self._home, self._home_place, self._fallback = saved, "", "last"
        self._gear = GearInference()
        self._tasks: list[asyncio.Task] = []
        self._cons: asyncio.Task | None = None
        self._t0 = 0.0
        self._cand_t0 = 0.0
        self._last_fix: float | None = None
        self._last_emit = -1e9
        self._prev: GeoPoint | None = None
        self.source = "none"
        self._cand_label = ""
        self._dirty = False
        self._pub: tuple | None = None
        self._last_obj: GpsFix | None = None
        self.gear = "P"

    # ── lifecycle ──
    def start(self) -> None:
        self._t0 = self._cand_t0 = self._clock()
        if self.spec.lower() == "sim":
            self._set_state("sim")
            return
        self._set_state("no_fix")
        self._tasks = [asyncio.ensure_future(self._run()), asyncio.ensure_future(self._watch())]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ── source loop ──
    async def _run(self) -> None:
        while True:
            for spec in self._candidates:
                self._cand_t0 = self._clock()
                self._cons = asyncio.ensure_future(self._consume(spec))
                try:
                    await asyncio.wait({self._cons})
                finally:
                    self._cons.cancel()
                if not self._cons.cancelled() and self._cons.exception():
                    log.info("GPS source %s: %s", spec, self._cons.exception())
            await self._sleep(RETRY_PAUSE_S)

    async def _consume(self, spec: str) -> None:
        label, stream = await self._open(spec)
        try:
            async for r in stream:
                if isinstance(r, GpsFix):
                    self._cand_label = label if isinstance(label, str) else (label or {}).get("label", spec)
                    self._on_fix(r)
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose:
                await aclose()

    async def _watch(self) -> None:
        while True:
            await self._sleep(WATCH_TICK_S)
            try:
                self._tick()
            except Exception:
                log.exception("GPS watchdog tick failed")

    def _tick(self) -> None:
        now = self._clock()
        ref = max(self._last_fix if self._last_fix is not None else self._t0, self._t0)
        if now - ref > self.timeout_s and self.vehicle.fix_state not in ("sim", "last"):
            self._to_sim()
        if self._cons and not self._cons.done() and now - max(self._cand_t0, self._last_fix or 0.0) > self.timeout_s:
            self._cons.cancel()
        if self._dirty and now - self._last_emit >= EMIT_INTERVAL_S:
            self._publish(now, self._last_obj)

    # ── state ──
    def _on_fix(self, fix: GpsFix) -> None:
        if fix.accuracy_m is not None and fix.accuracy_m > self.max_accuracy_m:
            return
        now = self._clock()
        self._last_fix = now
        v = self.vehicle
        here = GeoPoint(lat=fix.lat, lon=fix.lon)
        speed = fix.speed_mps or 0.0
        if fix.track is not None and speed >= MIN_HEADING_SPEED_MPS:
            v.heading = fix.track % 360
            self._prev = here
        elif self._prev is None:
            self._prev = here
        elif haversine(self._prev, here) >= MIN_HEADING_MOVE_M:
            v.heading = bearing(self._prev, here) % 360
            self._prev = here
        v.lat, v.lon, v.speed_mps = fix.lat, fix.lon, speed
        self.gear = self._gear.update(speed, now)
        self.source = self._cand_label or self.source
        self._last_obj = fix
        entering = v.fix_state != "fix"
        v.fix_state = "fix"
        if entering:
            self._publish(now, fix)
        elif self._differs():
            self._dirty = True
            if now - self._last_emit >= EMIT_INTERVAL_S:
                self._publish(now, fix)

    def _to_sim(self) -> None:
        v = self.vehicle
        if self._last_fix is not None:          # had a real fix this run: stay put, never jump away
            v.speed_mps = 0.0
            self._prev = None
            self._set_state("last")
            return
        v.lat, v.lon = self._home
        if self._home_place:
            v.place = self._home_place
        v.speed_mps = 0.0
        self._prev = None
        self._set_state(self._fallback)

    def _set_state(self, state: str) -> None:
        self.vehicle.fix_state = state
        self._publish(self._clock())

    def _differs(self) -> bool:
        v = self.vehicle
        if self._pub is None:
            return True
        lat, lon, hdg = self._pub
        dh = abs((v.heading - hdg + 180) % 360 - 180)
        return abs(v.lat - lat) > MOVE_EPS_DEG or abs(v.lon - lon) > MOVE_EPS_DEG or dh > HEADING_EPS_DEG

    def _publish(self, now: float, fix: GpsFix | None = None) -> None:
        v = self.vehicle
        self._last_emit = now
        self._dirty = False
        self._pub = (v.lat, v.lon, v.heading)
        if fix is not None and v.fix_state == "fix" and now - self._last_saved >= SAVE_INTERVAL_S:
            self._last_saved = now
            _save_last_fix(self._store, v.lat, v.lon)
        self.emit("gps", {
            "fix_state": v.fix_state, "badge": BADGES[v.fix_state], "lat": v.lat, "lon": v.lon,
            "heading": v.heading, "speed_mps": v.speed_mps, "gear": self.gear, "source": self.source,
            "accuracy_m": fix.accuracy_m if fix else None, "satellites": fix.satellites if fix else None})
        self.emit("vehicle", v.snapshot())
