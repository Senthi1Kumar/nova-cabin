"""Places, geocoding, routing and map rendering.

Mapbox (token) → Search Box / Geocoding v6 / Directions / Static Images
Google  (key)  → Places (New) text search for POIs
Keyless        → Nominatim, OSRM, CARTO dark tiles composited locally

Rendered maps are cached as PNG files; QML loads them by file URL.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx

from ..config import Settings

log = logging.getLogger("maps")
UA = "CabinAssistant/1.0 (in-vehicle HMI; contact: dev@elevatics.ai)"


@dataclass
class Place:
    name: str
    lat: float
    lon: float
    address: str = ""
    category: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Route:
    distance_m: float
    duration_s: float
    coords: list[tuple[float, float]]          # (lat, lon)


# ── geometry ──────────────────────────────────────────────────────
def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    R = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def decode_polyline(s: str, precision: int = 5) -> list[tuple[float, float]]:
    coords, i, lat, lon, f = [], 0, 0, 0, 10 ** precision
    while i < len(s):
        for which in (0, 1):
            shift = res = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                res |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(res >> 1) if res & 1 else res >> 1
            if which == 0:
                lat += d
            else:
                lon += d
        coords.append((lat / f, lon / f))
    return coords


def encode_polyline(coords: list[tuple[float, float]], precision: int = 5) -> str:
    out, plat, plon, f = [], 0, 0, 10 ** precision
    for lat, lon in coords:
        for v, p in ((round(lat * f), plat), (round(lon * f), plon)):
            d = v - p
            d = ~(d << 1) if d < 0 else d << 1
            while d >= 0x20:
                out.append(chr((0x20 | (d & 0x1F)) + 63))
                d >>= 5
            out.append(chr(d + 63))
        plat, plon = round(lat * f), round(lon * f)
    return "".join(out)


def simplify(coords: list[tuple[float, float]], max_pts: int = 120) -> list[tuple[float, float]]:
    if len(coords) <= max_pts:
        return coords
    step = len(coords) / max_pts
    out = [coords[int(i * step)] for i in range(max_pts)]
    out.append(coords[-1])
    return out


def point_along(coords: list[tuple[float, float]], frac: float) -> tuple[float, float]:
    segs = [haversine_m(coords[i], coords[i + 1]) for i in range(len(coords) - 1)]
    total, target, acc = sum(segs), sum(segs) * frac, 0.0
    for i, d in enumerate(segs):
        if acc + d >= target and d > 0:
            t = (target - acc) / d
            a, b = coords[i], coords[i + 1]
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        acc += d
    return coords[-1] if total else coords[0]


# ── service ───────────────────────────────────────────────────────
class Maps:
    def __init__(self, cfg: Settings, http: httpx.AsyncClient, cache: Path) -> None:
        self.cfg, self.http = cfg, http
        self.cache = cache
        self.cache.mkdir(parents=True, exist_ok=True)
        self.mapbox = cfg.get("MAPBOX_ACCESS_TOKEN")
        self.google = cfg.get("GOOGLE_MAPS_API_KEY")
        pref = cfg.get("MAP_PROVIDER", "auto").lower()
        self.use_mapbox = bool(self.mapbox) and pref in ("auto", "mapbox")
        self.style = cfg.get("MAPBOX_STYLE", "mapbox/navigation-night-v1")
        self._nominatim_lock = asyncio.Lock()   # 1 req/s policy

    attribution = "© OpenStreetMap © CARTO"

    @property
    def label(self) -> str:
        return "Mapbox" if self.use_mapbox else "OpenStreetMap"

    _fonts: dict = {}

    @classmethod
    def _font(cls, px: int):
        from PIL import ImageFont
        if px not in cls._fonts:
            f = Path(__file__).resolve().parents[2] / "assets" / "fonts" / "Inter-Medium.otf"
            try:
                cls._fonts[px] = ImageFont.truetype(str(f), px)
            except OSError:
                cls._fonts[px] = ImageFont.load_default()
        return cls._fonts[px]

    # ── search ──
    async def search(self, query: str, near: tuple[float, float], limit: int = 5) -> list[Place]:
        errors = []
        for fn in ([self._google_places] if self.google else []) + \
                  ([self._mapbox_search] if self.use_mapbox else []) + [self._nominatim]:
            try:
                res = await fn(query, near, limit)
                if res:
                    res.sort(key=lambda p: haversine_m(near, (p.lat, p.lon)) if p.extra.get("nearby") else 0)
                    return res
            except Exception as e:                                   # noqa: BLE001
                errors.append(f"{fn.__name__}: {e}")
        if errors:
            log.warning("place search failed: %s", errors)
        return []

    async def geocode(self, query: str, near: tuple[float, float]) -> Place | None:
        r = await self.search(query, near, 1)
        return r[0] if r else None

    async def _google_places(self, q, near, limit):
        r = await self.http.post(
            "https://places.googleapis.com/v1/places:searchText",
            headers={"X-Goog-Api-Key": self.google,
                     "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.location,"
                                         "places.rating,places.primaryTypeDisplayName,places.nationalPhoneNumber"},
            json={"textQuery": q, "maxResultCount": limit,
                  "locationBias": {"circle": {"center": {"latitude": near[0], "longitude": near[1]},
                                              "radius": 30000.0}}})
        r.raise_for_status()
        out = []
        for p in r.json().get("places", []):
            out.append(Place(p["displayName"]["text"], p["location"]["latitude"], p["location"]["longitude"],
                             p.get("formattedAddress", ""), (p.get("primaryTypeDisplayName") or {}).get("text", ""),
                             {"rating": p.get("rating"), "phone": p.get("nationalPhoneNumber"), "nearby": True}))
        return out

    async def _mapbox_search(self, q, near, limit):
        r = await self.http.get("https://api.mapbox.com/search/searchbox/v1/forward",
                                params={"q": q, "proximity": f"{near[1]},{near[0]}", "limit": limit,
                                        "access_token": self.mapbox})
        r.raise_for_status()
        feats = r.json().get("features", [])
        if not feats:
            r = await self.http.get("https://api.mapbox.com/search/geocode/v6/forward",
                                    params={"q": q, "proximity": f"{near[1]},{near[0]}", "limit": limit,
                                            "access_token": self.mapbox})
            r.raise_for_status()
            feats = r.json().get("features", [])
        out = []
        for f in feats:
            lon, lat = f["geometry"]["coordinates"]
            pr = f.get("properties", {})
            cats = pr.get("poi_category") or []
            out.append(Place(pr.get("name") or pr.get("name_preferred") or q, lat, lon,
                             pr.get("full_address") or pr.get("place_formatted", ""),
                             cats[0] if cats else pr.get("feature_type", ""),
                             {"nearby": bool(cats)}))
        return out

    async def _nominatim(self, q, near, limit):
        async with self._nominatim_lock:
            vb = f"{near[1]-0.6},{near[0]+0.45},{near[1]+0.6},{near[0]-0.45}"
            r = await self.http.get("https://nominatim.openstreetmap.org/search",
                                    params={"q": q, "format": "jsonv2", "limit": limit, "viewbox": vb,
                                            "addressdetails": 1},
                                    headers={"User-Agent": UA})
            r.raise_for_status()
            await asyncio.sleep(1.0)
        out = []
        for p in r.json():
            name = p.get("name") or p.get("display_name", q).split(",")[0]
            out.append(Place(name, float(p["lat"]), float(p["lon"]),
                             ", ".join(p.get("display_name", "").split(", ")[1:4]),
                             p.get("type", "").replace("_", " "), {"nearby": p.get("category") == "amenity"}))
        return out

    # ── routing ──
    async def route(self, a: tuple[float, float], b: tuple[float, float]) -> Route:
        if self.use_mapbox:
            try:
                r = await self.http.get(
                    f"https://api.mapbox.com/directions/v5/mapbox/driving/{a[1]},{a[0]};{b[1]},{b[0]}",
                    params={"geometries": "polyline", "overview": "simplified", "access_token": self.mapbox})
                r.raise_for_status()
                rt = r.json()["routes"][0]
                return Route(rt["distance"], rt["duration"], decode_polyline(rt["geometry"]))
            except Exception as e:                                   # noqa: BLE001
                log.warning("mapbox directions failed: %s", e)
        r = await self.http.get(f"https://router.project-osrm.org/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}",
                                params={"overview": "simplified", "geometries": "polyline"},
                                headers={"User-Agent": UA})
        r.raise_for_status()
        rt = r.json()["routes"][0]
        return Route(rt["distance"], rt["duration"], decode_polyline(rt["geometry"]))

    # ── static map rendering ──
    async def render(self, *, w: int, h: int, pins: list[tuple[float, float, str]] | None = None,
                     route: list[tuple[float, float]] | None = None,
                     center: tuple[float, float] | None = None, zoom: float | None = None) -> str:
        """Returns a file URL of a PNG at 2x density. pins: (lat, lon, kind) kind ∈ car|dest|poi|charge."""
        pins = pins or []
        key = hashlib.sha1(repr((w, h, pins, route and simplify(route, 60), center, zoom, self.use_mapbox,
                                 self.style)).encode()).hexdigest()[:16]
        path = self.cache / f"map_{key}.png"
        if path.exists():
            return path.as_uri()
        data = None
        if self.use_mapbox:
            try:
                data = await self._mapbox_static(w, h, pins, route, center, zoom)
            except Exception as e:                                   # noqa: BLE001
                log.warning("mapbox static failed, compositing tiles: %s", e)
        if data is None:
            data = await self._tiles_static(w, h, pins, route, center, zoom)
        path.write_bytes(data)
        return path.as_uri()

    _PIN = {"car": "4cd6ff", "dest": "ffb25e", "poi": "ffb25e", "charge": "ffd27a"}

    async def _mapbox_static(self, w, h, pins, route, center, zoom) -> bytes:
        ov = []
        if route:
            enc = quote(encode_polyline(simplify(route, 90)), safe="")
            ov.append(f"path-6+7c86ff-0.95({enc})")
        for lat, lon, kind in pins:
            size = "l" if kind in ("dest",) else "s"
            ov.append(f"pin-{size}+{self._PIN.get(kind, 'ffb25e')}({lon:.5f},{lat:.5f})")
        pos = "auto" if (ov and not center) else f"{center[1]:.5f},{center[0]:.5f},{zoom or 13}"
        url = (f"https://api.mapbox.com/styles/v1/{self.style}/static/{','.join(ov) + '/' if ov else ''}"
               f"{pos}/{min(w, 1280)}x{min(h, 1280)}@2x")
        params = {"access_token": self.mapbox, "attribution": "true", "logo": "false"}
        if pos == "auto":                      # Mapbox rejects padding with an explicit centre (422)
            params["padding"] = "60,60,60,60"
        r = await self.http.get(url, params=params)
        r.raise_for_status()
        return r.content

    async def _tiles_static(self, w, h, pins, route, center, zoom) -> bytes:
        from PIL import Image, ImageDraw, ImageFilter

        S = 2                                          # density
        W, H = w * S, h * S
        pts = [(p[0], p[1]) for p in pins] + (route or [])
        if center is None:
            center = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)) if pts else (0, 0)
        if zoom is None:
            zoom = 14.0
            if len(pts) > 1:
                for z in range(16, 2, -1):
                    xs = [self._px(lat, lon, z)[0] for lat, lon in pts]
                    ys = [self._px(lat, lon, z)[1] for lat, lon in pts]
                    mx, my = min(120, w * 0.18), min(120, h * 0.22)       # padding scales with map size
                    if max(xs) - min(xs) < (w - mx) and max(ys) - min(ys) < (h - my):
                        zoom = z
                        break
                cx = (max(self._px(a, b, zoom)[0] for a, b in pts) + min(self._px(a, b, zoom)[0] for a, b in pts)) / 2
                cy = (max(self._px(a, b, zoom)[1] for a, b in pts) + min(self._px(a, b, zoom)[1] for a, b in pts)) / 2
                center = self._unpx(cx, cy, zoom)
        z = int(zoom)
        cx, cy = self._px(*center, z)
        x0, y0 = cx - w / 2, cy - h / 2
        img = Image.new("RGB", (W, H), (14, 18, 30))
        tiles = [(tx, ty) for tx in range(int(x0 // 256), int((x0 + w) // 256) + 1)
                 for ty in range(int(y0 // 256), int((y0 + h) // 256) + 1)]

        async def fetch(tx, ty):
            n = 2 ** z
            if not 0 <= ty < n:
                return None
            data = await self._tile(z, tx % n, ty)
            return (tx, ty, data) if data else None

        results = await asyncio.gather(*(fetch(*t) for t in tiles))
        if not any(results):                           # offline: draw a quiet street-grid backdrop
            g = ImageDraw.Draw(img)
            step = 48 * S
            for i in range(-(int(x0 * S) % step), W, step):
                g.line([(i, 0), (i, H)], fill=(24, 31, 52), width=1)
            for j in range(-(int(y0 * S) % step), H, step):
                g.line([(0, j), (W, j)], fill=(24, 31, 52), width=1)
        for res in results:
            if res:
                tx, ty, data = res
                tile = Image.open(io.BytesIO(data)).convert("RGB")
                img.paste(tile, (int((tx * 256 - x0) * S), int((ty * 256 - y0) * S)))

        # tint toward the cabin palette
        tint = Image.new("RGB", img.size, (20, 30, 70))
        img = Image.blend(img, tint, 0.18)
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        to = lambda la, lo: ((self._px(la, lo, z)[0] - x0) * S, (self._px(la, lo, z)[1] - y0) * S)
        if route and len(route) > 1:
            line = [to(*p) for p in simplify(route, 400)]
            glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(glow).line(line, fill=(124, 134, 255, 200), width=18 * S // 2, joint="curve")
            layer.alpha_composite(glow.filter(ImageFilter.GaussianBlur(10)))
            d.line(line, fill=(150, 160, 255, 255), width=5 * S // 2 + 2, joint="curve")
        se = self._unpx(x0 + w, y0 + h, z)
        nw = self._unpx(x0, y0, z)
        placed: list[tuple[float, float]] = []
        for lat, lon, text in self._labels(z, (se[0], nw[1], nw[0], se[1])):
            x, y = to(lat, lon)
            if any(abs(x - px) < 90 * S and abs(y - py) < 18 * S for px, py in placed) or x > W - 60 * S:
                continue
            placed.append((x, y))
            d.ellipse((x - 3 * S, y - 3 * S, x + 3 * S, y + 3 * S), fill=(140, 152, 200, 200))
            d.text((x + 7 * S, y - 7 * S), text, fill=(170, 182, 225, 230), font=self._font(11 * S))
        for lat, lon, kind in pins:
            x, y = to(lat, lon)
            col = tuple(int(self._PIN.get(kind, "ffb25e")[i:i + 2], 16) for i in (0, 2, 4))
            r = 11 * S if kind == "dest" else 7 * S
            halo = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(halo).ellipse((x - r * 2.2, y - r * 2.2, x + r * 2.2, y + r * 2.2), fill=col + (110,))
            layer.alpha_composite(halo.filter(ImageFilter.GaussianBlur(r)))
            d.ellipse((x - r, y - r, x + r, y + r), fill=col + (255,), outline=(255, 255, 255, 255), width=2 * S)
        img = img.convert("RGBA")
        img.alpha_composite(layer)
        d = ImageDraw.Draw(img)
        txt = self.attribution
        d.text((W - 8 * S - len(txt) * 6, H - 16 * S), txt, fill=(150, 160, 190, 200))
        b = io.BytesIO()
        img.convert("RGB").save(b, "PNG", optimize=True)
        return b.getvalue()

    async def _tile(self, z: int, x: int, y: int) -> bytes | None:
        """One 512 px (@2x) raster tile. Override to supply tiles from elsewhere."""
        sub = "abcd"[(x + y) % 4]
        try:
            r = await self.http.get(f"https://{sub}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
                                    headers={"User-Agent": UA})
            r.raise_for_status()
            return r.content
        except Exception:                                            # noqa: BLE001
            return None

    def _labels(self, z: int, view: tuple[float, float, float, float]) -> list[tuple[float, float, str]]:
        """(lat, lon, text) labels to draw on composited maps. view = (s, w, n, e)."""
        return []

    @staticmethod
    def _px(lat, lon, z):
        s = 256 * 2 ** z
        x = (lon + 180) / 360 * s
        siny = min(max(math.sin(math.radians(lat)), -0.9999), 0.9999)
        y = (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * s
        return x, y

    @staticmethod
    def _unpx(x, y, z):
        s = 256 * 2 ** z
        lon = x / s * 360 - 180
        n = math.pi - 2 * math.pi * y / s
        lat = math.degrees(math.atan(math.sinh(n)))
        return lat, lon
