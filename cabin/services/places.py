"""Mapbox Search Box places ranked by Matrix drive time.

``within_min`` keeps only points inside the Mapbox isochrone. Tests pass a recorded HTTP client;
this module never logs the access token.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from driveos.domain.models import GeoPoint
from driveos.merchants.mapbox_pois import clean_features
from driveos.nav.geo import encode_polyline, haversine
from driveos.nav.mapbox import API, parse_search_feature

from .driveos_shop import Photos

log = logging.getLogger("places")

CATEGORY_IDS = {
    "coffee": "coffee", "cafe": "cafe", "food": "restaurant", "restaurant": "restaurant",
    "shopping": "shopping_mall", "mall": "shopping_mall", "fuel": "gas_station", "gas": "gas_station",
    "ev": "charging_station", "charging": "charging_station", "pharmacy": "pharmacy",
    "store": "grocery", "grocery": "grocery", "sandwich": "sandwich_shop", "bakery": "bakery", "bar": "bar",
}
# One cabin category can cover more than one Search Box id. Shopping includes malls and department stores
# so a small city still returns a handful of places inside a short drive.
CATEGORY_GROUPS = {"shopping": ["shopping_mall", "department_store"], "mall": ["shopping_mall", "department_store"]}
_COVER_TAG = {
    "coffee": "coffee", "cafe": "coffee", "restaurant": "food", "food": "food", "shopping": "store",
    "mall": "store", "fuel": "gas", "gas": "gas", "ev": "ev", "charging": "ev", "pharmacy": "pharmacy",
    "store": "store", "grocery": "store", "bakery": "bakery", "bar": "bar", "sandwich": "sandwich",
}
_CLEAN = {"coffee", "food", "gas", "ev", "store", "pharmacy", "bar", "bakery", "sandwich"}
_WITHIN = re.compile(r"within\s+(\d+)\s+min", re.IGNORECASE)


class PlacesOffline(Exception):
    """Mapbox is not configured or did not answer. Callers fall back to the local catalogue."""


@dataclass(frozen=True)
class Place:
    id: str
    name: str
    category: str
    lat: float
    lon: float
    address: str
    distance_m: float
    drive_s: float
    rating: float
    cover_uri: str
    as_of: str


def within_minutes(*parts: str) -> int | None:
    match = _WITHIN.search(" ".join(p for p in parts if p))
    return int(match.group(1)) if match else None


def live_category(category: str, query: str) -> str:
    blob = f"{category} {query}".lower()
    if any(w in blob for w in ("shopping centre", "shopping center", "shopping mall", "mall")):
        return "shopping"
    if "shopping" in blob and not (category or "").strip():
        return "shopping"
    if (category or "").strip():
        return category.strip()
    for key in ("coffee", "pharmacy", "fuel", "grocery", "restaurant"):
        if key in blob:
            return key
    if "charg" in blob:
        return "ev"
    if re.search(r"\b(gas|petrol|diesel)\b", blob):
        return "fuel"
    return "food"


class PlacesService:
    def __init__(self, http, token: str, cache_db: Path | str | None = None):
        self.http = http
        self.token = token or ""
        self.cache_db = cache_db
        self.photos = Photos()

    async def search(self, category: str, *, near: tuple[float, float], along: list[tuple[float, float]] | None = None,
                     within_min: int | None = None, limit: int = 6) -> list[Place]:
        if not self.token:
            raise PlacesOffline("no token")
        cats = CATEGORY_GROUPS.get(category, [CATEGORY_IDS.get(category, category)])
        lat, lon = near
        fetch = 25 if within_min else min(25, max(limit + 4, limit))
        params: dict[str, str] = {
            "limit": str(fetch),
            "proximity": f"{lon:.6f},{lat:.6f}",
            "language": "en",
        }
        if along and len(along) >= 2:
            thin = _thin(along, 100)
            params["route"] = encode_polyline([GeoPoint(lat=a, lon=b) for a, b in thin], 6)
            params["route_geometry"] = "polyline6"
            params["sar_type"] = "isochrone"
            params["time_deviation"] = str(within_min or 10)
        origin = GeoPoint(lat=lat, lon=lon)
        polygon = None
        if within_min:
            iso = await self._get(
                f"{API}/isochrone/v1/mapbox/driving/{lon:.6f},{lat:.6f}",
                {"contours_minutes": str(within_min), "polygons": "true", "denoise": "1"})
            polygon = _rings(iso)
            if not polygon:
                raise PlacesOffline("isochrone")
        feats = []
        seen: set[str] = set()
        for cat in cats:
            data = await self._get(f"{API}/search/searchbox/v1/category/{cat}", params)
            for raw in data.get("features") or []:
                feat = parse_search_feature(raw)
                key = str(feat.get("id") or feat.get("name") or "")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                feats.append(feat)
        feats = clean_features(feats, category if category in _CLEAN else "food")
        feats.sort(key=lambda f: haversine(origin, f["location"]))
        feats = feats[:24]
        drives, distances = await self._matrix(origin, feats)
        budget = within_min * 60 if within_min else None
        ranked = []
        for i, feat in enumerate(feats):
            loc = feat["location"]
            road = distances[i] if i < len(distances) and distances[i] is not None else None
            crow = feat.get("distance_m")
            if crow is None:
                crow = haversine(origin, loc)
            dist = float(road) if road is not None else float(crow)
            straight = float(crow) / 7.0
            reported = drives[i] if i < len(drives) and drives[i] is not None else None
            eta = straight if reported is None else float(reported)
            if budget is not None:
                # A Matrix time is authoritative. The polygon, then the straight-line crawl, only
                # decide when Matrix gave no time for this place.
                if reported is not None:
                    fits = eta <= budget
                elif polygon is not None:
                    fits = _inside(loc.lon, loc.lat, polygon)
                else:
                    fits = straight <= budget
                if not fits:
                    continue
            ranked.append((eta, dist, feat))
        ranked.sort(key=lambda row: (row[0], row[1]))
        as_of = datetime.now(UTC).replace(microsecond=0).isoformat()
        used: set[str] = set()
        tag = _COVER_TAG.get(category, "food")
        out: list[Place] = []
        for drive, dist, feat in ranked[:limit]:
            loc = feat["location"]
            cover = self.photos.cover(
                {"id": str(feat.get("id") or feat.get("name") or "place"), "name": str(feat.get("name") or ""),
                 "tags": [tag], "description": ""}, used)
            rating = feat.get("rating")
            out.append(Place(
                id=str(feat.get("id") or feat.get("name")),
                name=str(feat.get("name") or "Place"),
                category=category,
                lat=float(loc.lat), lon=float(loc.lon),
                address=str(feat.get("address") or ""),
                distance_m=float(dist), drive_s=float(drive),
                rating=float(rating) if rating is not None else 4.5,
                cover_uri=cover, as_of=as_of))
        return out

    async def _matrix(self, origin: GeoPoint, feats: list[dict]) -> tuple[list, list]:
        if not feats:
            return [], []
        coords = ";".join(
            [f"{origin.lon:.6f},{origin.lat:.6f}"]
            + [f"{f['location'].lon:.6f},{f['location'].lat:.6f}" for f in feats])
        data = await self._get(
            f"{API}/directions-matrix/v1/mapbox/driving/{coords}",
            {"sources": "0", "annotations": "duration,distance"})
        durations = (data.get("durations") or [[]])[0]
        distances = (data.get("distances") or [[]])[0]
        return list(durations[1:]), list(distances[1:])

    async def _get(self, url: str, params: dict[str, str]) -> dict:
        query = {**params, "access_token": self.token}
        try:
            try:
                resp = await self.http.request("GET", url, params=query)
            except TypeError:
                resp = await self.http.get(url, params=query)
        except PlacesOffline:
            raise
        except (httpx.HTTPError, OSError, TimeoutError, ValueError, TypeError) as exc:
            log.warning("places request failed: %s", type(exc).__name__)
            raise PlacesOffline(type(exc).__name__) from None
        code = getattr(resp, "status_code", 0)
        if code != 200:
            raise PlacesOffline(f"HTTP {code}")
        try:
            body = resp.json()
        except (ValueError, TypeError) as exc:
            raise PlacesOffline(type(exc).__name__) from None
        if not isinstance(body, dict):
            raise PlacesOffline("bad payload")
        return body


def _thin(points: list[tuple[float, float]], max_points: int) -> list[tuple[float, float]]:
    if len(points) <= max_points:
        return points
    step = (len(points) - 1) / (max_points - 1)
    out = [points[round(i * step)] for i in range(max_points - 1)]
    out.append(points[-1])
    return out


def _rings(iso: dict) -> list[list[tuple[float, float]]]:
    rings = []
    for feat in iso.get("features") or []:
        geom = feat.get("geometry") or {}
        kind = geom.get("type")
        coords = geom.get("coordinates") or []
        if kind == "Polygon":
            rings.extend(_ring(c) for c in coords if c)
        elif kind == "MultiPolygon":
            for poly in coords:
                rings.extend(_ring(c) for c in poly if c)
    return [r for r in rings if len(r) >= 3]


def _ring(coords) -> list[tuple[float, float]]:
    return [(float(c[0]), float(c[1])) for c in coords]


def _inside(lon: float, lat: float, rings: list[list[tuple[float, float]]]) -> bool:
    return any(_pip(lon, lat, ring) for ring in rings)


def _pip(lon: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside
