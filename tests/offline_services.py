"""Patch external geo/search/weather calls with fixtures (for CI and offline demos).
Map rendering still runs for real (tile fetch fails → styled blank map with route + pins)."""
from cabin.services import info, maps
from cabin.services.maps import Place, Route

TAHOE = Place("Lake Tahoe", 38.9399, -119.9772, "South Lake Tahoe, CA", "lake")
GGB = Place("Golden Gate Bridge", 37.8199, -122.4783, "San Francisco, CA", "landmark")
CAFES = [Place("Blue Bottle Coffee", 37.7825, -122.4078, "66 Mint St, San Francisco", "cafe", {"rating": 4.5, "nearby": True}),
         Place("Sightglass Coffee", 37.7770, -122.4085, "270 7th St, San Francisco", "cafe", {"rating": 4.6, "nearby": True}),
         Place("Philz Coffee", 37.7816, -122.4160, "Golden Gate Ave, San Francisco", "cafe", {"rating": 4.4, "nearby": True}),
         Place("Ritual Coffee", 37.7765, -122.4241, "Hayes St, San Francisco", "cafe", {"rating": 4.3, "nearby": True})]
RENO = [{"name": "Reno Supercharger", "town": "Reno", "lat": 39.5296, "lon": -119.8138, "kw": 250, "stalls": 12}]


def install():
    async def search(self, q, near, limit=5):
        q = q.lower()
        if "tahoe" in q: return [TAHOE]
        if "golden" in q: return [GGB]
        if "coffee" in q: return CAFES[:limit]
        return []

    async def route(self, a, b):
        pts = [a, (38.58, -121.49), (39.33, -120.18), (39.53, -119.81), b] if b[1] > -121 else [a, (37.80, -122.45), b]
        d = sum(maps.haversine_m(pts[i], pts[i + 1]) for i in range(len(pts) - 1)) * 1.12
        return Route(d, d / 24.5, pts)

    async def forecast(self, lat, lon, days=3):
        return {"unit": "°F", "now": {"temp": 57, "cond": "Mostly sunny", "wind": 6, "humidity": 41},
                "days": [{"day": d, "lo": lo, "hi": hi, "cond": c, "rain": r} for d, lo, hi, c, r in
                         [("Today", 48, 60, "Mostly sunny", 0), ("Saturday", 46, 62, "Mostly sunny", 5),
                          ("Sunday", 44, 58, "Partly cloudy", 10), ("Monday", 41, 52, "Light snow", 60)][:days]]}

    async def run(self, q, n=5):
        return {"answer": "Chipmakers announced new on-device AI accelerators.", "image": "",
                "results": [{"title": "New on-device AI chips announced", "url": "https://example.com/a",
                             "snippet": "…", "source": "theverge.com"},
                            {"title": "What the latest silicon means for cars", "url": "https://example.com/b",
                             "snippet": "…", "source": "arstechnica.com"},
                            {"title": "Markets react to AI hardware news", "url": "https://example.com/c",
                             "snippet": "…", "source": "reuters.com"},
                            {"title": "Hands-on with the new dev kits", "url": "https://example.com/d",
                             "snippet": "…", "source": "techcrunch.com"}]}

    async def chargers(self, lat, lon, n=5):
        return RENO

    maps.Maps.search = search
    maps.Maps.route = route
    info.Weather.forecast = forecast
    info.Search.run = run
    info.Charging.near = chargers
