"""Turn-by-turn navigation on a Mapbox route, spoken between turns."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from driveos.domain.models import GeoPoint
from driveos.nav.geo import with_cumulative
from driveos.nav.guidance import (
    CueTracker,
    EtaRefresh,
    OffRouteDetector,
    RerouteGate,
    lanes_at,
)

from ..services.catalog_gen import in_india
from .guidance import project_fix


@dataclass
class Nav:
    name: str
    address: str
    eta: str
    duration: str
    distance: str
    next_maneuver: str
    lanes: list
    arrival: str
    active: bool = True

    def as_dict(self) -> dict:
        return {
            "active": self.active, "name": self.name, "address": self.address, "eta": self.eta,
            "duration": self.duration, "distance": self.distance, "next_maneuver": self.next_maneuver,
            "lanes": self.lanes, "arrival": self.arrival,
        }


class NavAgent:
    def __init__(self, engine, mapbox, *, clock=None):
        self.engine = engine
        self.mapbox = mapbox
        self.clock = clock or _now
        self.route = None
        self.dest = None
        self.cues: CueTracker | None = None
        self.off = OffRouteDetector()
        self.gate = RerouteGate()
        self.eta_refresh = EtaRefresh()
        self.active = False
        self.metric = False
        self._http = getattr(mapbox, "http", None)

    async def close(self) -> None:
        http = self._http
        aclose = getattr(http, "aclose", None)
        if aclose is not None:
            await aclose()

    async def start(self, dest) -> Nav:
        origin = _point(self.engine.vehicle.lat, self.engine.vehicle.lon)
        target = _point(dest.lat, dest.lon)
        self.metric = in_india(float(dest.lat), float(dest.lon)) or in_india(origin.lat, origin.lon)
        self.dest = dest
        self.route = with_cumulative(await self._directions(origin, target, getattr(dest, "name", "")))
        self.cues = CueTracker(self.route)
        self.off.reset()
        self.eta_refresh.reset(self.clock())
        self.active = True
        nav = self._view(None)
        self.engine.emit("nav", nav.as_dict())
        return nav

    async def stop(self) -> None:
        self.active = False
        self.route = None
        self.cues = None
        self.engine.emit("nav", {"active": False})

    async def tick(self, fix) -> None:
        if not self.active or self.route is None or self.cues is None:
            return
        lat, lon = _fix(fix)
        pr = project_fix(self.route, lat, lon)
        now = self.clock()
        cue = self.cues.poll(pr.along_m, now)
        if cue:
            await self._say(cue)
        off_route = self.off.observe(pr.offset_m) and self.gate.allow(now)
        refresh = (not off_route) and self.eta_refresh.due(now) and self.gate.allow(now)
        if off_route or refresh:
            await self._reroute(lat, lon, now)
        self.engine.emit("nav", self._view(pr).as_dict())

    async def _reroute(self, lat: float, lon: float, now: float) -> None:
        self.gate.start()
        try:
            name = getattr(self.dest, "name", "")
            fresh = await self._directions(_point(lat, lon), _point(self.dest.lat, self.dest.lon), name)
            self.route = with_cumulative(fresh)
            self.cues = CueTracker(self.route)
            self.off.reset()
            self.eta_refresh.reset(now)
        finally:
            self.gate.done(now)

    async def _directions(self, origin: GeoPoint, dest: GeoPoint, name: str):
        return await self.mapbox.directions([origin, dest], names=["", name or ""])

    async def _say(self, text: str) -> None:
        fn = getattr(self.engine, "say_cue", None)
        if fn is None:
            self.engine.emit("nav_cue", {"text": text})
            return
        result = fn(text)
        if hasattr(result, "__await__"):
            await result

    def _view(self, pr) -> Nav:
        route, dest = self.route, self.dest
        along = pr.along_m if pr is not None else 0.0
        remaining_m = max(0.0, route.distance_m - along)
        remaining_s = route.duration_s * (remaining_m / route.distance_m) if route.distance_m else 0.0
        nxt, left = _upcoming(route, along)
        lanes = lanes_at(nxt, left) if nxt is not None else []
        instruction = ""
        if nxt is not None and nxt.maneuver is not None:
            instruction = nxt.maneuver.instruction or nxt.banner or ""
        return Nav(
            name=getattr(dest, "name", ""), address=getattr(dest, "address", "") or "",
            eta=_eta(remaining_s), duration=_duration(remaining_s), distance=_distance(remaining_m, self.metric),
            next_maneuver=instruction, lanes=lanes, arrival="")


def _point(lat, lon) -> GeoPoint:
    return GeoPoint(lat=float(lat), lon=float(lon))


def _fix(fix) -> tuple[float, float]:
    if hasattr(fix, "lat"):
        return float(fix.lat), float(fix.lon)
    if isinstance(fix, dict):
        return float(fix["lat"]), float(fix["lon"])
    return float(fix[0]), float(fix[1])


def _now() -> float:
    return datetime.now().timestamp()


def _upcoming(route, along: float):
    n = len(route.cumulative_m)
    idx = 0
    for i, step in enumerate(route.steps):
        start = route.cumulative_m[min(step.geometry_index, n - 1)] if n else 0.0
        if start <= along + 1.0:
            idx = i
    nxt = route.steps[idx + 1] if idx + 1 < len(route.steps) else None
    if nxt is None or not n:
        return nxt, 0.0
    end = route.cumulative_m[min(nxt.geometry_index, n - 1)]
    return nxt, max(0.0, end - along)


def _distance(metres: float, metric: bool) -> str:
    if metric:
        return f"{metres / 1000:.1f} km" if metres >= 1000 else f"{metres:.0f} m"
    miles = metres / 1609.344
    return f"{miles:.1f} mi"


def _duration(seconds: float) -> str:
    minutes = max(1, round(seconds / 60)) if seconds else 0
    if minutes >= 60:
        return f"{minutes // 60} h {minutes % 60} min"
    return f"{minutes} min"


def _eta(seconds: float) -> str:
    arrive = datetime.now() + timedelta(seconds=seconds)
    return arrive.strftime("%-I:%M %p")
