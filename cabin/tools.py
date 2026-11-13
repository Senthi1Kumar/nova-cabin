"""LLM tools → real lookups → (compact result for the model, card for the screen)."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import math
from dataclasses import asdict, dataclass

from .services.info import Charging, Search, Weather
from .services.maps import Maps, Place, haversine_m, point_along, simplify

log = logging.getLogger("tools")

M_PER_MI = 1609.344


@dataclass
class Vehicle:
    name: str
    lat: float
    lon: float
    place: str
    battery_pct: float
    range_full_mi: float
    cabin_temp_f: float
    seat_heat: int = 0
    fan: int = 3
    ac_on: bool = True
    fix_state: str = "sim"
    heading: float = 0.0
    speed_mps: float = 0.0

    @property
    def range_mi(self) -> float:
        return self.battery_pct / 100 * self.range_full_mi

    @property
    def pos(self) -> tuple[float, float]:
        return (self.lat, self.lon)

    def snapshot(self) -> dict:
        d = asdict(self)
        d["range_mi"] = round(self.range_mi)
        return d


SCHEMAS = [
    {"type": "function", "function": {
        "name": "plan_trip",
        "description": "Plan a drive to a destination: route distance and time, EV charging stops, arrival "
                       "battery and destination weather. Shows a trip card with the route map.",
        "parameters": {"type": "object", "properties": {
            "destination": {"type": "string", "description": "Place name or address"},
            "when": {"type": "string", "description": "Optional departure, e.g. 'Saturday 9am'"}},
            "required": ["destination"]}}},
    {"type": "function", "function": {
        "name": "find_places",
        "description": "Find businesses, points of interest or an address and show them on a map "
                       "(restaurants, coffee, chargers, parking, landmarks, a specific address).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to find, e.g. 'sushi', 'Golden Gate Bridge'"},
            "near": {"type": "string", "description": "Optional area; omit for near the car"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web for current facts, news, opening hours, sports, prices, or anything "
                       "you are not sure about. Shows sources on screen.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "Current weather and 3-day forecast for a place (default: the car's location).",
        "parameters": {"type": "object", "properties": {"location": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "navigate_to",
        "description": "Start turn-by-turn navigation to a destination now.",
        "parameters": {"type": "object", "properties": {"destination": {"type": "string"}},
                       "required": ["destination"]}}},
    {"type": "function", "function": {
        "name": "set_climate",
        "description": "Adjust cabin climate: temperature in °F, seat heater level 0-3, fan 0-10, A/C on/off.",
        "parameters": {"type": "object", "properties": {
            "temperature_f": {"type": "number"}, "seat_heat": {"type": "integer", "minimum": 0, "maximum": 3},
            "fan": {"type": "integer", "minimum": 0, "maximum": 10}, "ac_on": {"type": "boolean"}}}}},
    {"type": "function", "function": {
        "name": "vehicle_status",
        "description": "Battery, range, cabin temperature and location of the car.",
        "parameters": {"type": "object", "properties": {}}}},
]

CAPTIONS = {"plan_trip": "Planning route", "find_places": "Searching nearby", "web_search": "Searching the web",
            "get_weather": "Checking weather", "navigate_to": "Routing", "set_climate": "Adjusting climate",
            "vehicle_status": "Reading vehicle"}


def fmt_dur(s: float) -> str:
    m = int(round(s / 60))
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


class Toolbox:
    def __init__(self, cfg, maps: Maps, search: Search, weather: Weather, charging: Charging, vehicle: Vehicle):
        self.cfg, self.maps, self.search, self.weather, self.charging = cfg, maps, search, weather, charging
        self.v = vehicle
        self.metric = cfg.metric
        self.schemas: list[dict] = list(SCHEMAS)
        self.captions: dict[str, str] = dict(CAPTIONS)
        self._extra: dict[str, object] = {}

    def extend(self, schemas: list[dict], methods: dict[str, object], captions: dict[str, str] | None = None) -> None:
        """Register more tools (extension point for commerce, payment, vehicle...). ``methods`` maps a tool
        name to ``async fn(**args) -> (result_for_llm, card | None, nav | None)``."""
        self.schemas.extend(schemas)
        self._extra.update(methods)
        if captions:
            self.captions.update(captions)

    # units
    def dist(self, m: float) -> str:
        if self.metric:
            return f"{m / 1000:.1f} km" if m < 10000 else f"{m / 1000:.0f} km"
        mi = m / M_PER_MI
        return f"{mi:.1f} mi" if mi < 10 else f"{mi:.0f} mi"

    async def run(self, name: str, args: dict) -> tuple[dict, dict | None, dict | None]:
        """Returns (result_for_llm, card, nav)."""
        fn = self._extra.get(name) or getattr(self, "t_" + name, None)
        if fn is None:
            return {"error": f"unknown tool {name}"}, None, None
        try:
            return await fn(**args)
        except TypeError as e:
            return {"error": f"bad arguments: {e}"}, None, None
        except Exception as e:                                       # noqa: BLE001
            log.exception("tool %s failed", name)
            return {"error": f"{type(e).__name__}: {e}"[:300]}, None, None

    async def _resolve(self, text: str) -> Place:
        p = await self.maps.geocode(text, self.v.pos)
        if not p:
            raise LookupError(f"could not find '{text}'")
        return p

    # ── plan_trip ────────────────────────────────────────────────
    async def t_plan_trip(self, destination: str, when: str = ""):
        dest = await self._resolve(destination)
        route = await self.maps.route(self.v.pos, (dest.lat, dest.lon))
        dist_mi = route.distance_m / M_PER_MI
        full = self.v.range_full_mi
        reserve = 0.10 * full
        stops, charge_min, charger = 0, 0, None
        if dist_mi <= self.v.range_mi - reserve:
            arrival = self.v.battery_pct - dist_mi / full * 100
        else:
            first_leg = max(self.v.range_mi - 0.15 * full, 0)
            remaining = dist_mi - first_leg
            per_leg = 0.65 * full                                   # 80% → 15%
            stops = 1 + max(0, math.ceil((remaining - (0.80 * full - reserve)) / per_leg))
            last_leg = remaining - (stops - 1) * per_leg
            arrival = 80 - last_leg / full * 100
            charge_min = 25 * stops
            at = point_along(route.coords, min(first_leg / dist_mi, 0.95) * 0.92)
            try:
                cs = await self.charging.near(*at)
                charger = cs[0] if cs else None
            except Exception as e:                                   # noqa: BLE001
                log.warning("charger lookup: %s", e)
        wx_txt, wx = "—", None
        try:
            wx = await self.weather.forecast(dest.lat, dest.lon, 3)
            lo = min(d["lo"] for d in wx["days"])
            hi = max(d["hi"] for d in wx["days"])
            wx_txt = f"{lo}–{hi}{wx['unit']}, {wx['days'][min(1, len(wx['days']) - 1)]['cond']}"
        except Exception as e:                                       # noqa: BLE001
            log.warning("weather: %s", e)
        total_s = route.duration_s + charge_min * 60
        eta = (dt.datetime.now() + dt.timedelta(seconds=total_s)).strftime("%-I:%M %p")
        pins = [(self.v.lat, self.v.lon, "car"), (dest.lat, dest.lon, "dest")]
        if charger:
            pins.insert(1, (charger["lat"], charger["lon"], "charge"))
        hero = await self.maps.render(w=392, h=196, pins=pins, route=route.coords)
        stop_txt = "Not needed" if not stops else \
            f"{stops} stop{'s' if stops > 1 else ''}" + (f" · {charger['town'] or charger['name']}" if charger else "") \
            + f", {charge_min} min"
        card = {
            "kind": "trip", "title": dest.name, "subtitle": dest.address,
            "hero": self._map_hero(hero, dest.name, pins, route.coords),
            "rows": [
                {"icon": "route", "label": "Route", "value": f"{self.dist(route.distance_m)} · {fmt_dur(route.duration_s)}"},
                {"icon": "bolt", "label": "Charging", "value": stop_txt, "tint": "#FFD27A"},
                {"icon": "battery", "label": "Arrival", "value": f"{max(arrival, 0):.0f}% battery", "tint": "#8EF0C4"},
                {"icon": "cloud", "label": "Weather", "value": wx_txt}],
            "actions": [
                {"key": "navigate", "text": "Start Navigation", "icon": "send", "primary": True,
                 "payload": {"name": dest.name, "lat": dest.lat, "lon": dest.lon}},
                {"key": "calendar", "text": "Add to Calendar", "icon": "calendar",
                 "payload": {"title": f"Drive to {dest.name}", "when": when, "duration_s": total_s,
                             "location": dest.address or dest.name}},
                {"key": "share", "text": "Share Plan", "icon": "share",
                 "payload": {"text": f"Trip to {dest.name}: {self.dist(route.distance_m)}, {fmt_dur(route.duration_s)}"
                                     f" + {charge_min} min charging. ETA {eta}. Weather {wx_txt}."}}],
        }
        result = {"destination": dest.name, "address": dest.address, "distance": self.dist(route.distance_m),
                  "drive_time": fmt_dur(route.duration_s), "charging_stops": stops,
                  "charge_location": charger and (charger["town"] or charger["name"]), "charge_minutes": charge_min,
                  "arrival_battery_pct": round(max(arrival, 0)), "eta_if_leaving_now": eta, "weather": wx_txt,
                  "departure": when or "now"}
        return result, card, None

    # ── find_places ──────────────────────────────────────────────
    async def t_find_places(self, query: str, near: str = ""):
        center = self.v.pos
        if near:
            c = await self._resolve(near)
            center = (c.lat, c.lon)
        places = await self.maps.search(query, center, 6)
        if not places:
            return {"results": [], "note": "nothing found"}, None, None
        places.sort(key=lambda p: haversine_m(self.v.pos, (p.lat, p.lon)))
        top = places[:4]
        far = haversine_m(self.v.pos, (top[0].lat, top[0].lon)) > 150_000
        pins = ([] if far else [(self.v.lat, self.v.lon, "car")]) + \
               [(p.lat, p.lon, "dest" if i == 0 else "poi") for i, p in enumerate(top)]
        hero = await self.maps.render(w=392, h=196, pins=pins, center=(top[0].lat, top[0].lon) if len(top) == 1 else None,
                                      zoom=14 if len(top) == 1 else None)
        rows, res = [], []
        for i, p in enumerate(top):
            d = haversine_m(self.v.pos, (p.lat, p.lon))
            extra = f" · ★ {p.extra['rating']}" if p.extra.get("rating") else ""
            rows.append({"icon": "pin", "label": self.dist(d), "value": p.name + extra,
                         "tint": "#FFB25E" if i == 0 else ""})
            res.append({"name": p.name, "address": p.address, "category": p.category, "distance": self.dist(d),
                        "rating": p.extra.get("rating")})
        f = top[0]
        actions = [{"key": "navigate", "text": "Navigate", "icon": "send", "primary": True,
                    "payload": {"name": f.name, "lat": f.lat, "lon": f.lon}}]
        if f.extra.get("phone"):
            actions.append({"key": "call", "text": "Call", "icon": "phone", "payload": {"phone": f.extra["phone"]}})
        actions.append({"key": "share", "text": "Share", "icon": "share",
                        "payload": {"text": f"{f.name} — {f.address}"}})
        card = {"kind": "place", "title": f.name, "subtitle": f.address,
                "hero": self._map_hero(hero, f.name, pins), "rows": rows, "actions": actions}
        return {"results": res}, card, None

    # ── web_search ───────────────────────────────────────────────
    async def t_web_search(self, query: str):
        r = await self.search.run(query, 5)
        if not r["results"]:
            return {"results": [], "note": "no results"}, None, None
        rows = [{"icon": "globe", "label": x["source"], "value": x["title"]} for x in r["results"][:4]]
        hero = {"kind": "image", "source": r["image"], "label": ""} if r.get("image") else None
        top = r["results"][0]
        card = {"kind": "search", "title": query, "subtitle": "", "hero": hero, "rows": rows,
                "sources": [x["source"] for x in r["results"][:5]],
                "actions": [{"key": "open", "text": "Open Source", "icon": "globe", "primary": True,
                             "payload": {"url": top["url"]}},
                            {"key": "share", "text": "Send to Phone", "icon": "share",
                             "payload": {"text": f"{top['title']} — {top['url']}"}}]}
        llm = {"answer": r.get("answer", "")[:800],
               "results": [{"title": x["title"], "source": x["source"], "snippet": x["snippet"][:300]}
                           for x in r["results"][:5]]}
        return llm, card, None

    # ── get_weather ──────────────────────────────────────────────
    async def t_get_weather(self, location: str = ""):
        if location:
            p = await self._resolve(location)
            lat, lon, name = p.lat, p.lon, p.name
        else:
            lat, lon, name = self.v.lat, self.v.lon, self.v.place
        wx = await self.weather.forecast(lat, lon, 4)
        u = wx["unit"]
        rows = [{"icon": "cloud", "label": d["day"],
                 "value": f"{d['lo']}–{d['hi']}{u}, {d['cond']}" + (f" · {d['rain']}% rain" if d.get("rain") else "")}
                for d in wx["days"][:4]]
        card = {"kind": "weather", "title": name, "subtitle": "",
                "hero": {"kind": "weather", "temp": f"{wx['now']['temp']}°", "cond": wx["now"]["cond"],
                         "label": name, "detail": f"Wind {wx['now']['wind']} {'km/h' if self.metric else 'mph'}"
                                                  f" · Humidity {wx['now']['humidity']}%"},
                "rows": rows, "actions": []}
        return {"location": name, **wx}, card, None

    # ── navigate_to ──────────────────────────────────────────────
    async def t_navigate_to(self, destination: str):
        dest = await self._resolve(destination)
        agent = getattr(self, "nav_agent", None)
        if agent is not None:
            try:
                started = await agent.start(dest)
                guided = started.as_dict() if hasattr(started, "as_dict") else dict(started or {})
            except Exception as exc:                                 # noqa: BLE001
                log.warning("guidance start: %s", type(exc).__name__)
                try:
                    await agent.stop()
                except Exception:                                   # noqa: BLE001
                    pass
                nav = await self.build_nav(dest.name, dest.lat, dest.lon, getattr(dest, "address", ""))
                return {"navigating_to": dest.name, "distance": nav["distance"], "eta": nav["eta"]}, None, nav
            try:
                drawn = await self.build_nav(dest.name, dest.lat, dest.lon, getattr(dest, "address", ""))
            except Exception as exc:                                 # noqa: BLE001
                log.warning("route map: %s", type(exc).__name__)
                drawn = {}
            nav = {**drawn, **{k: v for k, v in guided.items() if v not in ("", None, [])}}
            return {"navigating_to": dest.name, "distance": nav.get("distance"), "eta": nav.get("eta")}, None, nav
        nav = await self.build_nav(dest.name, dest.lat, dest.lon, dest.address)
        return {"navigating_to": dest.name, "distance": nav["distance"], "eta": nav["eta"]}, None, nav

    def _map_hero(self, source, label, pins, route=None):
        hero = {"kind": "map", "source": source, "label": label,
                "pins": [[float(lat), float(lon), str(role)] for lat, lon, role in pins]}
        if route:
            hero["route"] = [[float(lat), float(lon)] for lat, lon in simplify(list(route), 120)]
        return hero

    async def build_nav(self, name, lat, lon, address=""):
        route = await self.maps.route(self.v.pos, (lat, lon))
        img = await self.maps.render(w=1060, h=600, pins=[(self.v.lat, self.v.lon, "car"), (lat, lon, "dest")],
                                     route=route.coords)
        eta = (dt.datetime.now() + dt.timedelta(seconds=route.duration_s)).strftime("%-I:%M %p")
        need = route.distance_m / M_PER_MI
        arrival = self.v.battery_pct - need / self.v.range_full_mi * 100
        pins = [(self.v.lat, self.v.lon, "car"), (lat, lon, "dest")]
        return {"name": name, "address": address, "map": img, "distance": self.dist(route.distance_m),
                "duration": fmt_dur(route.duration_s), "eta": eta,
                "arrival": f"{max(arrival, 0):.0f}%" if arrival > 5 else "Charge needed",
                "pins": [[a, b, c] for a, b, c in pins],
                "route": [[a, b] for a, b in simplify(route.coords, 120)]}

    # ── vehicle ──────────────────────────────────────────────────
    async def t_set_climate(self, temperature_f: float | None = None, seat_heat: int | None = None,
                            fan: int | None = None, ac_on: bool | None = None):
        if temperature_f is not None:
            t = float(temperature_f)
            if t < 45:                                       # model sent °C
                t = t * 9 / 5 + 32
            self.v.cabin_temp_f = max(60, min(85, round(t)))
        if seat_heat is not None:
            self.v.seat_heat = max(0, min(3, int(seat_heat)))
        if fan is not None:
            self.v.fan = max(0, min(10, int(fan)))
        if ac_on is not None:
            self.v.ac_on = bool(ac_on)
        return {"ok": True, **self.v.snapshot()}, None, None

    async def t_vehicle_status(self):
        v = self.v
        rows = [{"icon": "battery", "label": "Battery", "value": f"{v.battery_pct:.0f}%", "tint": "#8EF0C4"},
                {"icon": "route", "label": "Range", "value": self.dist(v.range_mi * M_PER_MI)},
                {"icon": "fan", "label": "Cabin", "value": f"{v.cabin_temp_f:.0f}°F · fan {v.fan}"},
                {"icon": "pin", "label": "Parked", "value": v.place}]
        hero = await self.maps.render(w=392, h=196, pins=[(v.lat, v.lon, "car")], center=v.pos, zoom=15)
        card = {"kind": "vehicle", "title": v.name, "subtitle": "",
                "hero": self._map_hero(hero, v.name, [(v.lat, v.lon, "car")]), "rows": rows, "actions": []}
        return v.snapshot(), card, None


def tool_result_json(r: dict) -> str:
    return json.dumps(r, ensure_ascii=False, default=str)[:6000]


async def gather_tools(box: Toolbox, calls: list[dict]):
    async def one(c):
        try:
            args = json.loads(c["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {}
        return c, await box.run(c["name"], args if isinstance(args, dict) else {})
    return await asyncio.gather(*(one(c) for c in calls))
