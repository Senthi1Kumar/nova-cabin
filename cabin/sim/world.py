"""Offline world model for simulation mode.

Deterministic (seeded) so the same question gives the same answer, and
plausible enough to exercise every UI path: places, POIs, routes, EV
chargers, weather and web results. Nothing here touches the network.
Business names are invented; coordinates of public places are real.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import random
import re

from ..services.maps import Place, Route, haversine_m

# name, lat, lon, address, kind, elevation_ft, aliases
GAZETTEER = [
    ("San Francisco", 37.7749, -122.4194, "San Francisco, CA", "city", 50, ["sf", "the city"]),
    ("Oakland", 37.8044, -122.2712, "Oakland, CA", "city", 40, []),
    ("Berkeley", 37.8716, -122.2727, "Berkeley, CA", "city", 170, []),
    ("San Jose", 37.3382, -121.8863, "San Jose, CA", "city", 80, []),
    ("Palo Alto", 37.4419, -122.1430, "Palo Alto, CA", "city", 30, []),
    ("Mountain View", 37.3861, -122.0839, "Mountain View, CA", "city", 100, []),
    ("Sausalito", 37.8591, -122.4853, "Sausalito, CA", "city", 20, []),
    ("Napa", 38.2975, -122.2869, "Napa, CA", "city", 20, ["napa valley", "wine country"]),
    ("Sonoma", 38.2919, -122.4580, "Sonoma, CA", "city", 80, []),
    ("Santa Cruz", 36.9741, -122.0308, "Santa Cruz, CA", "city", 20, []),
    ("Monterey", 36.6002, -121.8947, "Monterey, CA", "city", 30, []),
    ("Carmel-by-the-Sea", 36.5552, -121.9233, "Carmel-by-the-Sea, CA", "city", 220, ["carmel"]),
    ("Big Sur", 36.2704, -121.8081, "Big Sur, CA", "area", 150, []),
    ("Half Moon Bay", 37.4636, -122.4286, "Half Moon Bay, CA", "city", 70, []),
    ("Sacramento", 38.5816, -121.4944, "Sacramento, CA", "city", 30, []),
    ("Lake Tahoe", 38.9399, -119.9772, "South Lake Tahoe, CA", "lake", 6230, ["tahoe", "south lake tahoe"]),
    ("Reno", 39.5296, -119.8138, "Reno, NV", "city", 4500, []),
    ("Truckee", 39.3280, -120.1833, "Truckee, CA", "city", 5820, []),
    ("Yosemite Valley", 37.7456, -119.5936, "Yosemite National Park, CA", "park", 4000, ["yosemite"]),
    ("Fresno", 36.7378, -119.7871, "Fresno, CA", "city", 310, []),
    ("Los Angeles", 34.0522, -118.2437, "Los Angeles, CA", "city", 300, ["la", "l.a."]),
    ("Santa Barbara", 34.4208, -119.6982, "Santa Barbara, CA", "city", 50, []),
    ("Las Vegas", 36.1699, -115.1398, "Las Vegas, NV", "city", 2000, ["vegas"]),
    ("Point Reyes", 38.0690, -122.8069, "Point Reyes Station, CA", "park", 30, []),
    ("Muir Woods", 37.8970, -122.5811, "Mill Valley, CA", "park", 200, []),
    ("Livermore", 37.6819, -121.7680, "Livermore, CA", "city", 480, []),
    ("Tracy", 37.7397, -121.4252, "Tracy, CA", "city", 60, []),
    ("Gilroy", 37.0058, -121.5683, "Gilroy, CA", "city", 200, []),
    ("Kettleman City", 36.0083, -119.9618, "Kettleman City, CA", "city", 240, []),
    ("San Luis Obispo", 35.2828, -120.6596, "San Luis Obispo, CA", "city", 230, ["slo"]),
    ("Auburn", 38.8966, -121.0769, "Auburn, CA", "city", 1250, []),
    ("Vacaville", 38.3566, -121.9877, "Vacaville, CA", "city", 180, []),
    ("Vallejo", 38.1041, -122.2566, "Vallejo, CA", "city", 50, []),
    ("Placerville", 38.7296, -120.7985, "Placerville, CA", "city", 1870, []),
    ("Merced", 37.3022, -120.4830, "Merced, CA", "city", 170, []),
    ("Barstow", 34.8958, -117.0173, "Barstow, CA", "city", 2180, []),
    ("Redding", 40.5865, -122.3917, "Redding, CA", "city", 560, []),
    ("Eugene", 44.0521, -123.0868, "Eugene, OR", "city", 430, []),
    ("Medford", 42.3265, -122.8756, "Medford, OR", "city", 1380, []),
    ("Baker", 35.2650, -116.0750, "Baker, CA", "city", 920, []),
    ("Modesto", 37.6391, -120.9969, "Modesto, CA", "city", 90, []),
    ("Salinas", 36.6777, -121.6555, "Salinas, CA", "city", 50, []),
    ("Portland", 45.5152, -122.6784, "Portland, OR", "city", 50, []),
    ("Seattle", 47.6062, -122.3321, "Seattle, WA", "city", 170, []),
    ("Golden Gate Bridge", 37.8199, -122.4783, "Golden Gate Bridge, San Francisco, CA", "landmark", 220,
     ["golden gate", "the bridge", "golden gate bridge"]),
    ("Fisherman's Wharf", 37.8080, -122.4177, "Jefferson St, San Francisco, CA", "landmark", 10, ["fishermans wharf", "pier 39"]),
    ("Union Square", 37.7880, -122.4075, "Union Square, San Francisco, CA", "landmark", 90, []),
    ("Ferry Building", 37.7955, -122.3937, "1 Ferry Building, San Francisco, CA", "landmark", 10, []),
    ("SFO Airport", 37.6213, -122.3790, "San Francisco International Airport, CA", "airport", 10,
     ["sfo", "the airport", "san francisco airport", "airport"]),
    ("Oakland Airport", 37.7126, -122.2197, "Oakland International Airport, CA", "airport", 10, ["oak"]),
    ("Stanford University", 37.4275, -122.1697, "450 Jane Stanford Way, Stanford, CA", "landmark", 100, ["stanford"]),
    ("Chase Center", 37.7680, -122.3877, "1 Warriors Way, San Francisco, CA", "landmark", 10, []),
    ("Oracle Park", 37.7786, -122.3893, "24 Willie Mays Plaza, San Francisco, CA", "landmark", 10, []),
    ("Levi's Stadium", 37.4030, -121.9700, "4900 Marie P DeBartolo Way, Santa Clara, CA", "landmark", 20, ["levis stadium"]),
    ("Twin Peaks", 37.7544, -122.4477, "Twin Peaks, San Francisco, CA", "landmark", 920, []),
]
CITIES = [g for g in GAZETTEER if g[4] in ("city", "lake")]

STREETS = ["Market", "Mission", "Valencia", "Hayes", "Grove", "Oak", "Pine", "Bush", "Sutter", "Folsom",
           "Howard", "Brannan", "Townsend", "Irving", "Judah", "Clement", "Geary", "Fillmore", "Polk", "Divisadero"]

POI = {
    "coffee": (["Ember & Bean", "Northline Coffee", "Fogline Roasters", "Common Grounds", "Little Kettle",
                "Harbor Espresso", "Driftwood Café", "Slow Pour Coffee"], "Coffee shop"),
    "sushi": (["Kumo Sushi", "Hikari Omakase", "Saba House", "Tide & Rice", "Umi Bar"], "Sushi restaurant"),
    "pizza": (["Crust & Ember", "Golden Slice", "Fog City Pizza", "Pie Society", "Basil Oven"], "Pizza"),
    "burger": (["Patty Lab", "Smash Republic", "Gold Rush Burgers", "Grillhouse 12"], "Burgers"),
    "tacos": (["Taqueria Luna", "El Farolito Verde", "Tacos del Puerto", "La Brasa"], "Mexican"),
    "restaurant": (["Juniper Table", "The Copper Fork", "Lantern House", "Saltwater Kitchen", "Olive & Ash",
                    "Northbeach Trattoria"], "Restaurant"),
    "bakery": (["Morning Crumb", "Sourdough Social", "Flour & Fog", "Sugarline Bakery"], "Bakery"),
    "charger": (["VoltHub Fast Charge", "Electra Charge Plaza", "GridPoint DC Fast", "Supercharger"], "EV charging"),
    "parking": (["Civic Center Garage", "Fifth & Mission Garage", "Harbor Parking", "Union Square Garage"], "Parking"),
    "gas": (["Coastline Fuel", "Bayview Gas", "Ridge Fuel Stop"], "Gas station"),
    "grocery": (["Fresh Coast Market", "Greenleaf Grocery", "Corner Harvest"], "Grocery"),
    "pharmacy": (["CareWell Pharmacy", "Bayside Drugs", "Pillbox Pharmacy"], "Pharmacy"),
    "hotel": (["The Meridian Hotel", "Harborlight Inn", "Hotel Fogline", "The Larkspur"], "Hotel"),
    "gym": (["Iron Bay Fitness", "Pulse Athletic Club", "Summit Gym"], "Gym"),
    "park": (["Cypress Park", "Lookout Point Park", "Bayfront Green"], "Park"),
    "car wash": (["Shine Bay Car Wash", "Spotless Auto Spa"], "Car wash"),
}
POI_ALIASES = {"cafe": "coffee", "espresso": "coffee", "latte": "coffee", "food": "restaurant", "eat": "restaurant",
               "dinner": "restaurant", "lunch": "restaurant", "breakfast": "bakery", "brunch": "restaurant",
               "charging": "charger", "chargers": "charger", "supercharger": "charger", "ev": "charger",
               "fuel": "gas", "petrol": "gas", "groceries": "grocery", "supermarket": "grocery",
               "drugstore": "pharmacy", "hotels": "hotel", "motel": "hotel", "burgers": "burger",
               "taco": "tacos", "mexican": "tacos", "japanese": "sushi", "italian": "pizza", "garage": "parking"}

WEB = [
    (r"\bev\b|electric (car|vehicle)|battery tech|charging news",
     "Solid-state battery pilot lines are ramping up, and several automakers announced 800-volt fast charging for next year's models.",
     [("Solid-state pilot lines begin shipping cells", "electrek.co"),
      ("800-volt charging comes to mainstream EVs", "insideevs.com"),
      ("What faster charging means for road trips", "caranddriver.com"),
      ("Charging network adds 2,000 stalls this quarter", "reuters.com")]),
    (r"tech|ai\b|chip|apple|google|nvidia",
     "Chipmakers announced new on-device AI accelerators aimed at cars and phones, promising faster assistants that work offline.",
     [("New on-device AI chips announced", "theverge.com"),
      ("What the latest silicon means for cars", "arstechnica.com"),
      ("Markets react to AI hardware news", "reuters.com"),
      ("Hands-on with the new developer kits", "techcrunch.com")]),
    (r"warriors|nba|basketball|lakers",
     "The Warriors won their last game 118 to 109, pulling away with a twelve-point fourth quarter.",
     [("Warriors pull away late for 118–109 win", "espn.com"),
      ("Box score and highlights", "nba.com"),
      ("Five takeaways from the fourth quarter", "theathletic.com")]),
    (r"49ers|niners|football|nfl",
     "The 49ers won 27 to 20 on Sunday, with a late interception sealing it.",
     [("49ers hold on for 27–20 win", "espn.com"), ("Game recap", "nfl.com"), ("Defense delivers again", "sfchronicle.com")]),
    (r"stock|market|nasdaq|dow|s&p",
     "Markets are mixed today: the Nasdaq is up about half a percent while the Dow is roughly flat.",
     [("Stocks mixed as tech leads", "cnbc.com"), ("Market snapshot", "bloomberg.com"), ("What's moving markets", "wsj.com")]),
    (r"traffic|accident|bridge closed|commute",
     "Traffic is moderate. There's a slowdown on the Bay Bridge westbound adding about ten minutes.",
     [("Bay Area traffic map", "511.org"), ("Live incidents", "sfgate.com"), ("Commute alerts", "kron4.com")]),
    (r"open|hours|closing time",
     "Most shops nearby are open until nine tonight; restaurants typically until ten.",
     [("Hours and info", "yelp.com"), ("Local business guide", "sfgate.com")]),
]

WMO_CONDS = ["Clear", "Mostly sunny", "Partly cloudy", "Overcast", "Light rain", "Showers", "Fog"]


def _seed(*parts) -> random.Random:
    return random.Random(int(hashlib.sha1(repr(parts).encode()).hexdigest()[:12], 16))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 &']", "", s.lower()).strip()


class World:
    def __init__(self, home: tuple[float, float]) -> None:
        self.home = home

    # ── places ───────────────────────────────────────────────────
    def nearest_city(self, lat: float, lon: float) -> str:
        return min(CITIES, key=lambda g: haversine_m((lat, lon), (g[1], g[2])))[0]

    def lookup(self, query: str) -> Place | None:
        q = _norm(query)
        q = re.sub(r"^(the )?", "", q)
        best = None
        for name, lat, lon, addr, kind, elev, aliases in GAZETTEER:
            keys = [_norm(name)] + [_norm(a) for a in aliases]
            if q in keys:
                return Place(name, lat, lon, addr, kind, {"elev": elev})
            if any(k and (k in q or q in k) for k in keys) and len(q) >= 3:
                best = best or Place(name, lat, lon, addr, kind, {"elev": elev})
        return best

    def _address(self, rng: random.Random, lat: float, lon: float) -> str:
        return f"{rng.randint(12, 2900)} {rng.choice(STREETS)} St, {self.nearest_city(lat, lon)}"

    def search(self, query: str, near: tuple[float, float], limit: int = 5) -> list[Place]:
        q = _norm(query)
        # a named place wins
        named = self.lookup(re.sub(r"\b(near|in|at|around)\b.*$", "", q).strip() or q)
        cat = next((c for c in POI if c in q), None) or next((POI_ALIASES[a] for a in POI_ALIASES if a in q.split()), None)
        if named and not cat:
            return [named]
        rng = _seed("poi", cat or q, round(near[0], 2), round(near[1], 2))
        names, label = POI.get(cat, ([], cat or "Place"))
        if not names:                                  # unknown query: invent a plausible match
            names = [query.strip().title()]
            label = "Place"
        out = []
        pool = names[:]
        rng.shuffle(pool)
        for i, n in enumerate(pool[:limit]):
            r = 250 + i * 380 + rng.random() * 400                     # metres, increasing
            a = rng.random() * 2 * math.pi
            lat = near[0] + r * math.cos(a) / 111_320
            lon = near[1] + r * math.sin(a) / (111_320 * math.cos(math.radians(near[0])))
            out.append(Place(n, lat, lon, self._address(rng, lat, lon), label,
                             {"rating": round(4.0 + rng.random() * 0.8, 1), "nearby": True,
                              "phone": f"(415) 555-01{rng.randint(10, 99)}"}))
        out.sort(key=lambda p: haversine_m(near, (p.lat, p.lon)))
        return out

    def geocode_or_invent(self, query: str) -> Place:
        p = self.lookup(query)
        if p:
            return p
        rng = _seed("invent", _norm(query))
        r, a = 3000 + rng.random() * 25000, rng.random() * 2 * math.pi
        lat = self.home[0] + r * math.cos(a) / 111_320
        lon = self.home[1] + r * math.sin(a) / (111_320 * math.cos(math.radians(self.home[0])))
        return Place(query.strip().title(), lat, lon, self._address(rng, lat, lon), "place", {"elev": 100})

    # ── routing ──────────────────────────────────────────────────
    def route(self, a: tuple[float, float], b: tuple[float, float]) -> Route:
        from .roads import route_polyline
        hwy = route_polyline(a, b)
        if hwy:
            length = sum(haversine_m(hwy[i], hwy[i + 1]) for i in range(len(hwy) - 1)) * 1.04
            return Route(length, length / 28.5, hwy)                      # ~64 mph average
        d = haversine_m(a, b)
        rng = _seed("route", round(a[0], 3), round(a[1], 3), round(b[0], 3), round(b[1], 3))
        n = max(12, min(80, int(d / 600)))
        bend = (rng.random() - 0.5) * 0.22
        pts = []
        for i in range(n + 1):
            t = i / n
            lat = a[0] + (b[0] - a[0]) * t
            lon = a[1] + (b[1] - a[1]) * t
            off = bend * math.sin(math.pi * t) + 0.02 * math.sin(t * 13 + rng.random()) * math.sin(math.pi * t)
            lat += -(b[1] - a[1]) * off
            lon += (b[0] - a[0]) * off
            pts.append((lat, lon))
        road = d * (1.18 if d > 30000 else 1.35)
        speed = 24.0 if d > 30000 else 10.5
        return Route(road, road / speed, pts)

    def chargers_near(self, lat: float, lon: float) -> list[dict]:
        town = self.nearest_city(lat, lon)
        g = next(g for g in CITIES if g[0] == town)
        rng = _seed("chg", town)
        return [{"name": f"{town} {rng.choice(['Supercharger', 'Fast Charge Hub', 'Charge Plaza'])}",
                 "town": town, "lat": g[1] + 0.004, "lon": g[2] - 0.003, "kw": rng.choice([150, 250, 350]),
                 "stalls": rng.choice([8, 12, 16, 20])}]

    # ── weather ──────────────────────────────────────────────────
    def weather(self, lat: float, lon: float, days: int, metric: bool) -> dict:
        elev = 0
        for g in GAZETTEER:
            if haversine_m((lat, lon), (g[1], g[2])) < 15000:
                elev = g[5]
                break
        today = dt.date.today()
        month = today.month
        season = math.cos((month - 7.5) / 12 * 2 * math.pi)            # 1 summer … -1 winter
        coastal = lon < -122.2 and lat < 38.6
        hi_base = (66 if coastal else 74) + season * (6 if coastal else 18) - elev / 1000 * 3.6 \
            - max(0.0, lat - 38.0) * 2.2 + max(0.0, 36.0 - lat) * 1.5          # cooler north, warmer south
        spread = 10 if coastal else 22
        out_days = []
        for i in range(days):
            rng = _seed("wx", round(lat, 1), round(lon, 1), (today + dt.timedelta(days=i)).isoformat())
            hi = hi_base + rng.uniform(-4, 4)
            lo = hi - spread + rng.uniform(-3, 3)
            w = [5, 6, 4, 2, 1.4 - season, 1 - season, 2.5 if coastal else 0.3]
            cond = rng.choices(WMO_CONDS, weights=[max(x, 0.1) for x in w])[0]
            if lo < 33 and cond in ("Light rain", "Showers"):
                cond = "Light snow"
            rain = {"Light rain": 60, "Showers": 70, "Light snow": 65, "Overcast": 20, "Fog": 10}.get(cond, 0)
            out_days.append({"day": "Today" if i == 0 else (today + dt.timedelta(days=i)).strftime("%A"),
                             "lo": lo, "hi": hi, "cond": cond, "rain": rain})
        rng = _seed("now", round(lat, 1), round(lon, 1), today.isoformat(), dt.datetime.now().hour)
        hour = dt.datetime.now().hour
        frac = max(0.0, math.sin((hour - 6) / 12 * math.pi))
        now_t = out_days[0]["lo"] + (out_days[0]["hi"] - out_days[0]["lo"]) * frac
        conv = (lambda f: (f - 32) * 5 / 9) if metric else (lambda f: f)
        for d in out_days:
            d["lo"], d["hi"] = round(conv(d["lo"])), round(conv(d["hi"]))
        return {"unit": "°C" if metric else "°F",
                "now": {"temp": round(conv(now_t)), "cond": out_days[0]["cond"],
                        "wind": rng.randint(3, 16), "humidity": rng.randint(35, 85)},
                "days": out_days}

    # ── web ──────────────────────────────────────────────────────
    def web(self, query: str) -> dict:
        q = query.lower()
        for pat, answer, results in WEB:
            if re.search(pat, q):
                return {"answer": answer, "image": "", "results": [
                    {"title": t, "url": f"https://{s}/sim/{abs(hash(t)) % 10**6}", "snippet": answer, "source": s}
                    for t, s in results]}
        topic = query.strip().rstrip("?").strip()
        p = self.lookup(topic)
        if p:
            ans = (f"{p.name} is in {p.address.split(',')[-2].strip() if ',' in p.address else p.address}"
                   f"{', at about ' + format(p.extra.get('elev', 0), ',') + ' feet' if p.extra.get('elev', 0) > 1000 else ''}.")
        else:
            ans = ""
        title = topic[:1].upper() + topic[1:]
        return {"answer": ans, "image": "", "results": [
            {"title": f"{title} — overview", "url": "https://en.wikipedia.org/wiki/Special:Search", "snippet": ans,
             "source": "wikipedia.org"},
            {"title": f"{title}: what to know", "url": "https://www.britannica.com", "snippet": "", "source": "britannica.com"},
            {"title": f"Latest on {topic.lower()}", "url": "https://apnews.com", "snippet": "", "source": "apnews.com"},
            {"title": f"{title} explained", "url": "https://www.vox.com", "snippet": "", "source": "vox.com"}]}
