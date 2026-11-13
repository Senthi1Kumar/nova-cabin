"""Service implementations backed by the offline World (same interfaces as cabin/services)."""
from __future__ import annotations

import asyncio
import random

from ..services.info import Charging, Search, Weather
from ..services.maps import Maps, Place, Route
from .tiles import render_tile
from .world import CITIES, GAZETTEER, World


def _latency(lo: float = 0.25, hi: float = 0.7) -> float:
    return random.uniform(lo, hi)


class SimMaps(Maps):
    attribution = "Simulated map"

    def __init__(self, cfg, http, cache, world: World, online_tiles: bool) -> None:
        super().__init__(cfg, http, cache)
        self.world = world
        self.use_mapbox = False
        self.google = ""
        self.online_tiles = online_tiles
        self.cache = cache / ("sim_online" if online_tiles else "sim")
        self.cache.mkdir(parents=True, exist_ok=True)

    @property
    def label(self) -> str:
        return "Simulated · CARTO tiles" if self.online_tiles else "Simulated map"

    async def search(self, query, near, limit=5) -> list[Place]:
        await asyncio.sleep(_latency())
        return self.world.search(query, near, limit)

    async def geocode(self, query, near) -> Place | None:
        await asyncio.sleep(_latency(0.1, 0.3))
        return self.world.geocode_or_invent(query)

    async def route(self, a, b) -> Route:
        await asyncio.sleep(_latency())
        return self.world.route(a, b)

    async def _tile(self, z, x, y):
        if self.online_tiles:
            data = await super()._tile(z, x, y)
            if data:
                return data
        path = self.cache / f"t_{z}_{x}_{y}.png"
        if path.exists():
            return path.read_bytes()
        data = await asyncio.to_thread(render_tile, z, x, y)
        path.write_bytes(data)
        return data

    def _labels(self, z, view):
        if self.online_tiles:
            return []                                   # CARTO tiles carry their own labels
        s, w, n, e = view
        out = []
        for g in GAZETTEER:
            big = g in CITIES
            if (big and z <= 12 or not big and z >= 13) and s < g[1] < n and w < g[2] < e:
                out.append((g[1], g[2], g[0]))
        return out[:14]


class SimSearch(Search):
    def __init__(self, cfg, http, cache, world: World) -> None:
        super().__init__(cfg, http, cache)
        self.world = world

    @property
    def label(self) -> str:
        return "Simulated web"

    async def run(self, query: str, n: int = 5) -> dict:
        await asyncio.sleep(_latency(0.4, 0.9))
        r = self.world.web(query)
        r["results"] = r["results"][:n]
        r["provider"] = "simulation"
        return r


class SimWeather(Weather):
    def __init__(self, cfg, http, world: World) -> None:
        super().__init__(cfg, http)
        self.world = world

    async def forecast(self, lat, lon, days=3) -> dict:
        await asyncio.sleep(_latency(0.15, 0.4))
        return self.world.weather(lat, lon, days, self.metric)


class SimCharging(Charging):
    def __init__(self, cfg, http, maps, world: World) -> None:
        super().__init__(cfg, http, maps)
        self.world = world

    async def near(self, lat, lon, n=5) -> list[dict]:
        await asyncio.sleep(_latency(0.1, 0.3))
        return self.world.chargers_near(lat, lon)
