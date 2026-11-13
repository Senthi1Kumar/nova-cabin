"""Navigation specialist: route, places and trip questions."""
from __future__ import annotations

import dataclasses
import json
import logging
import re

from .base import Agent, AgentRequest

log = logging.getLogger("agents.nav")

_GO = re.compile(r"^\s*(?:please\s+)?(?:navigate|route|drive|take me|head|go|directions?)\b(?:\s+to)?\s+(?P<dest>.+?)\s*$",
                 re.IGNORECASE)


class NavigationAgent(Agent):
    description = ("Navigation specialist: start or stop a route to a destination, or find places, landmarks and "
                   "addresses near the car.")

    def __init__(self, engine) -> None:
        super().__init__("nav", engine)
        self._nav = None
        self._fallback_logged = False

    async def execute(self, request: AgentRequest) -> tuple[dict, str]:
        ctx = request.context
        action = str(ctx.get("action") or "")
        dest = str(ctx.get("destination") or "").strip()
        if not dest and not action:
            m = _GO.match(request.request)
            dest = m.group("dest") if m else ""
        if action == "stop":
            return await self._stop()
        if dest and action in ("", "navigate"):
            out = await self._start(dest)
            if out is not None:
                return out
            res, _card, nav = await self.engine.tools.run("navigate_to", {"destination": dest})
            if nav is not None and not res.get("error"):
                self.engine.nav_open = True
                self.engine.emit("nav", nav)
            return self._pack(res, f"Navigating to {res.get('navigating_to', dest)}")
        return await self._find(request)

    # ── planned navigation (NavAgent + PlacesService) ─────────
    def _planner(self):
        """The navigation track's NavAgent, or None when its pieces are not installed."""
        if self._nav is not None:
            return self._nav
        try:
            from ..nav.agent import NavAgent
        except ImportError:
            return self._note_fallback("cabin.nav.agent missing")
        mapbox = getattr(self.engine, "mapbox", None)
        if mapbox is None:
            return self._note_fallback("engine.mapbox not set")
        self._nav = NavAgent(self.engine, mapbox)
        return self._nav

    def _note_fallback(self, why: str):
        if not self._fallback_logged:
            log.info("navigation uses the plain tools (%s)", why)
            self._fallback_logged = True

    async def _resolve(self, query: str):
        pos = self.engine.vehicle.pos
        places = getattr(self.engine, "places", None)
        if places is not None:
            try:
                found = await places.search(query, near=pos, limit=1)
            except Exception as e:                                   # noqa: BLE001
                log.info("places search unavailable (%s)", type(e).__name__)
                found = []
            if found:
                return found[0]
        maps = getattr(self.engine, "maps", None)
        if maps is not None:
            try:
                return await maps.geocode(query, pos)
            except Exception as e:                                   # noqa: BLE001
                log.info("geocode unavailable (%s)", type(e).__name__)
        return None

    async def _start(self, dest: str):
        nav = self._planner()
        if nav is None:
            return None
        place = await self._resolve(dest)
        if place is None:
            return None
        started = await nav.start(place)
        ui = await self.engine.tools.build_nav(place.name, place.lat, place.lon, getattr(place, "address", "") or "")
        for key, value in _plain(started).items():
            ui.setdefault(key, value)
        self.engine.nav_open = True
        self.engine.emit("nav", ui)
        return {"navigating_to": place.name, "nav": {k: v for k, v in ui.items() if k != "map"}}, \
            f"Navigating to {place.name}"

    async def _stop(self) -> tuple[dict, str]:
        if self._nav is not None:
            await _maybe_await(self._nav.stop())
        self.engine.nav_open = False
        return {"stopped": True}, "Navigation stopped"

    # ── places ────────────────────────────────────────────────
    async def _find(self, request: AgentRequest) -> tuple[dict, str]:
        ctx = request.context
        query = str(ctx.get("query") or request.request).strip()
        args = {"query": query}
        if ctx.get("near"):
            args["near"] = str(ctx["near"])
        tool = "shop_find_places" if ctx.get("orderable") else "find_places"
        res, card, _nav = await self.engine.tools.run(tool, args)
        if card is not None and not res.get("error"):
            self.engine.emit("card", card)
        found = res.get("results") or []
        names = ", ".join(str(p.get("name", "")) for p in found[:3] if isinstance(p, dict))
        return self._pack(res, f"{len(found)} places found" + (f": {names}" if names else ""))

    @staticmethod
    def _pack(res: dict, summary: str) -> tuple[dict, str]:
        if res.get("error"):
            raise RuntimeError(res["error"])
        return dict(res), summary


def _plain(obj) -> dict:
    """The JSON-serialisable fields of a guidance object (dataclass or dict); everything else is dropped."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        obj = {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    if not isinstance(obj, dict):
        return {}
    out = {}
    for key, value in obj.items():
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue
        out[str(key)] = value
    return out


async def _maybe_await(value):
    if hasattr(value, "__await__"):
        return await value
    return value
