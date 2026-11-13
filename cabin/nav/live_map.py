"""MapLibre view of the current GPS fix, drawn by DriveOS's off-screen map worker.

The shared library is built on the Pi against the MapLibre package DriveOS already
links. When that library is absent the panel keeps the still image and the app
still starts.
"""
from __future__ import annotations

import ctypes
import logging
import math
import os
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, QTimer
from PySide6.QtGui import QImage, QPainter
from PySide6.QtQml import qmlRegisterType
from PySide6.QtQuick import QQuickImageProvider, QQuickPaintedItem

log = logging.getLogger("nav.map")

_PIN_M = 30.0
HOT_PERIOD_MS = 250
_PIN_SPEED_MPS = 1.0

def _meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = p1 - p2, math.radians(lon1 - lon2)
    h = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * 6_371_000.0 * math.asin(math.sqrt(h))


def pinned_position(held: tuple[float, float] | None, lat: float, lon: float, speed_mps: float) -> tuple[float, float]:
    """Hold a stopped fix. A weak satellite lock wanders less than this and must not slide the puck."""
    if held is not None and speed_mps < _PIN_SPEED_MPS and _meters(held[0], held[1], lat, lon) < _PIN_M:
        return held
    return (lat, lon)

_EMPTY = '{"type":"FeatureCollection","features":[]}'
_HERE = {"name": "Current location", "eta": "—", "duration": "—", "distance": "—", "arrival": "—"}
_ROOT = Path(__file__).resolve().parents[2]
_lib = None
_looked = False
_session = None
_provider = None


def library_path() -> Path | None:
    env = os.environ.get("NOVA_MAP_LIB", "")
    if env and Path(env).is_file():
        return Path(env)
    for rel in ("mapservice/build/libnova_map.so", "packaging/map-service/libnova_map.so"):
        path = _ROOT / rel
        if path.is_file():
            return path
    return None


def load_library():
    """The MapLibre worker, or None when this machine has no build of it."""
    global _lib, _looked
    if _looked:
        return _lib
    _looked = True
    path = library_path()
    if path is None:
        return None
    try:
        lib = ctypes.CDLL(str(path))
    except OSError:
        log.warning("live map library could not be loaded")
        return None
    lib.nova_map_create.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_char_p,
                                    ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32]
    lib.nova_map_create.restype = ctypes.c_void_p
    lib.nova_map_destroy.argtypes = [ctypes.c_void_p]
    lib.nova_map_set_period.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    lib.nova_map_set_car.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_double]
    lib.nova_map_follow.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_double,
                                    ctypes.c_double, ctypes.c_double, ctypes.c_double]
    lib.nova_map_set_route.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    for name, types in (
        ("nova_map_set_pins", [ctypes.c_void_p, ctypes.c_char_p]),
        ("nova_map_fit", [ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_double,
                          ctypes.c_double, ctypes.c_double, ctypes.c_double]),
        ("nova_map_camera", [ctypes.c_void_p, ctypes.c_double, ctypes.c_double,
                             ctypes.c_double, ctypes.c_double]),
    ):
        fn = getattr(lib, name, None)
        if fn is not None:
            fn.argtypes = types
    lib.nova_map_take.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
                                  ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
    lib.nova_map_take.restype = ctypes.POINTER(ctypes.c_uint8)
    lib.nova_map_failed.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
    lib.nova_map_failed.restype = ctypes.c_int
    _lib = lib
    return lib


def route_geojson(points, limit: int = 400) -> str:
    """One blue line. Points are GeoPoints or (lat, lon) pairs, thinned to `limit`."""
    coords: list[tuple[float, float]] = []
    for point in points or ():
        lon = getattr(point, "lon", None)
        lat = getattr(point, "lat", None)
        if lon is None:
            if point is None or len(point) < 2:
                continue
            lat, lon = point[0], point[1]
        coords.append((float(lon), float(lat)))
    if len(coords) > limit:
        step = max(1, len(coords) // limit)
        kept = coords[::step]
        if kept[-1] != coords[-1]:
            kept.append(coords[-1])
        coords = kept
    if len(coords) < 2:
        return _EMPTY
    body = ",".join(f"[{lon:.6f},{lat:.6f}]" for lon, lat in coords)
    return ('{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"c":"u"},'
            '"geometry":{"type":"LineString","coordinates":[' + body + ']}}]}')


_KINDS = {"dest", "poi", "charge"}


def pins_geojson(pins) -> str:
    """Place dots. The car is the live fix, so a car pin is not drawn again."""
    features: list[str] = []
    for pin in pins or ():
        if pin is None or len(pin) < 2:
            continue
        kind = str(pin[2]) if len(pin) > 2 else "poi"
        if kind == "car":
            continue
        if kind not in _KINDS:
            kind = "poi"
        features.append(
            '{"type":"Feature","properties":{"k":"%s"},"geometry":{"type":"Point","coordinates":[%.6f,%.6f]}}'
            % (kind, float(pin[1]), float(pin[0])))
    if not features:
        return _EMPTY
    return '{"type":"FeatureCollection","features":[' + ",".join(features) + "]}"


def frame_bounds(points):
    """South, west, north, east around the places, or None when they are one spot."""
    lats: list[float] = []
    lons: list[float] = []
    for point in points or ():
        lat = getattr(point, "lat", None)
        lon = getattr(point, "lon", None)
        if lat is None:
            if point is None or len(point) < 2:
                continue
            lat, lon = float(point[0]), float(point[1])
        lats.append(float(lat))
        lons.append(float(lon))
    if not lats:
        return None
    south, north = min(lats), max(lats)
    west, east = min(lons), max(lons)
    if (north - south) < 1e-5 and (east - west) < 1e-5:
        return None
    pad_lat = max((north - south) * 0.08, 0.002)
    pad_lon = max((east - west) * 0.08, 0.002)
    return south - pad_lat, west - pad_lon, north + pad_lat, east + pad_lon


class LiveMap:
    """Owns the worker. The GUI thread only copies finished frames."""

    def __init__(self, engine) -> None:
        global _session
        self.engine = engine
        self._lib = None
        self._map = None
        self._shown = False
        self._preview = False
        self._follow_only = False
        self._dismissed = False
        self._live = False
        self._pins: list = []
        self._preview_route: list = []
        self._nav_route: list = []
        self._fix: dict | None = None
        self._pinned: tuple[float, float] | None = None
        self._warned = False
        _session = self
        if _provider is not None:
            _provider.session = self

    @property
    def available(self) -> bool:
        return load_library() is not None

    def show(self, announce: bool = True) -> bool:
        self._dismissed = False
        self._shown = True
        self._preview = False
        self._follow_only = bool(announce)
        if announce:
            self._nav_route = []
        self.engine.emit("map_open", True)
        if announce and not self._has_destination():
            self.engine.emit("nav", dict(_HERE))
        ok = self._ensure()
        self._apply_pace()
        return ok

    def hide(self) -> None:
        self._shown = False
        self._dismissed = True
        self._follow_only = False
        if self._pins or self._preview_route:
            self._preview = True
            if self._map and self._lib:
                self._frame_preview()
        self._apply_pace()
        self.engine.emit("map_open", False)

    def preview(self, pins, route=None) -> None:
        """Fit the reply card to these places. Does not open the nav page."""
        self._pins = [tuple(pin) for pin in (pins or [])]
        self._preview_route = list(route or [])
        if self._shown:
            return
        self._preview = True
        self._follow_only = False
        self._ensure()
        self._apply_pace()

    def on_card(self, value) -> None:
        hero = value.get("hero") if isinstance(value, dict) else None
        if not isinstance(hero, dict) or hero.get("kind") != "map":
            self.stop_preview()
            return
        self.preview(hero.get("pins") or [], hero.get("route"))

    def stop_preview(self) -> None:
        was = self._preview
        self._preview = False
        if self._shown:
            return
        self._pins = []
        self._preview_route = []
        if was:
            self._apply_pace()

    def on_gps(self, fix) -> None:
        if not isinstance(fix, dict) or "lat" not in fix or "lon" not in fix:
            return
        lat, lon = pinned_position(self._pinned, float(fix["lat"]), float(fix["lon"]),
                                   float(fix.get("speed_mps") or 0.0))
        self._pinned = (lat, lon)
        self._fix = {**fix, "lat": lat, "lon": lon}
        if self._map and self._lib:
            self._lib.nova_map_set_car(self._map, lat, lon, float(fix.get("heading") or 0.0))
        if self._shown or self._preview:
            self._apply_pace()

    def clear(self) -> None:
        """Forget the last route and pins so the next trip never shows an old line."""
        self._nav_route, self._pins, self._preview_route = [], [], []
        self._preview = False
        if self._map and self._lib:
            self._push_route()
        self._apply_pace()

    def on_nav(self, value) -> None:
        if isinstance(value, dict) and value.get("active") is False:
            self.clear()
            return
        if not isinstance(value, dict) or not value.get("map"):
            return
        route = value.get("route")
        if route:
            self._nav_route = list(route)
        pins = value.get("pins")
        if pins:
            self._pins = list(pins)
        self._dismissed = False
        self.show(announce=False)

    async def close(self) -> None:
        lib, handle, self._map = self._lib, self._map, None
        self._shown = False
        if lib is not None and handle:
            lib.nova_map_destroy(handle)

    def pull(self):
        """One BGRA frame copied out of the worker, or None when it has nothing new."""
        if self._shown or self._preview:
            self._apply_pace()
        if not self._map or not self._lib or not self._rendering():
            return None
        w, h, stride = ctypes.c_uint32(), ctypes.c_uint32(), ctypes.c_uint32()
        ptr = self._lib.nova_map_take(self._map, ctypes.byref(w), ctypes.byref(h), ctypes.byref(stride))
        if not ptr or not w.value or not h.value:
            return None
        step = stride.value or w.value * 4
        return w.value, h.value, step, ctypes.string_at(ptr, step * h.value)

    def _rendering(self) -> bool:
        return bool((self._shown or self._preview) and self._map)

    def _heading_up(self) -> bool:
        """North-up by default: a weak fix makes the heading swing and the map spin."""
        cfg = getattr(self.engine, "cfg", None)
        return bool(cfg is not None and str(cfg.get("MAP_HEADING_UP", "off")).lower() in ("1", "on", "true", "yes"))

    def _hot(self) -> bool:
        flags = getattr(self.engine, "thermal_flags", None) or {}
        return bool(flags.get("map_service_off"))

    def _has_destination(self) -> bool:
        agent = getattr(getattr(self.engine, "nav", None), "agent", None)
        return bool(getattr(agent, "active", False) or getattr(self.engine, "nav_open", False))

    def _token(self) -> str:
        cfg = getattr(self.engine, "cfg", None)
        if cfg is None:
            return ""
        return str(cfg.get("MAPBOX_ACCESS_TOKEN") or "")

    def _position(self) -> tuple[float, float, float]:
        if self._fix:
            return float(self._fix["lat"]), float(self._fix["lon"]), float(self._fix.get("heading") or 0.0)
        vehicle = self.engine.vehicle
        return (float(getattr(vehicle, "lat", 0.0) or 0.0), float(getattr(vehicle, "lon", 0.0) or 0.0),
                float(getattr(vehicle, "heading", 0.0) or 0.0))

    def _ensure(self) -> bool:
        if self._map:
            self._place_car()
            self._aim()
            return True
        lib = load_library()
        if lib is None:
            if not self._warned:
                self._warned = True
                log.info("live map unavailable; the panel keeps the still image")
            return False
        self._lib = lib
        lat, lon, _heading = self._position()
        cache_dir = Path.home() / ".cache" / "nova-cabin" / "maplibre"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache = cache_dir / "cache.db"
        self._map = lib.nova_map_create(lat, lon, self._token().encode(), str(cache).encode(), 1024, 600)
        if not self._map:
            log.warning("live map failed to start")
            return False
        why = ctypes.create_string_buffer(256)
        if lib.nova_map_failed(self._map, why, 256) and why.value:
            log.warning("live map: %s", why.value.decode(errors="replace"))
        self._place_car()
        self._aim()
        return True

    def _place_car(self) -> None:
        if not self._map or not self._lib:
            return
        lat, lon, heading = self._position()
        self._lib.nova_map_set_car(self._map, lat, lon, heading)

    def _fn(self, name: str):
        if self._lib is None:
            return None
        try:
            return getattr(self._lib, name)
        except AttributeError:
            return None

    def _aim(self) -> None:
        if self._preview and not self._shown:
            self._frame_preview()
        else:
            self._push_route()

    def _line(self) -> list:
        agent = getattr(getattr(self.engine, "nav", None), "agent", None)
        route = getattr(agent, "route", None) if getattr(agent, "active", False) else None
        points = list(getattr(route, "geometry", None) or [])
        if len(points) >= 2:
            return points
        if self._nav_route:
            return list(self._nav_route)
        if not self._follow_only:
            return list(self._preview_route)
        return []

    def _push_route(self) -> None:
        if not self._map or not self._lib:
            return
        points = self._line()
        self._lib.nova_map_set_route(self._map, route_geojson(points).encode())
        pins = self._fn("nova_map_set_pins")
        if pins is not None:
            pins(self._map, pins_geojson([] if self._follow_only else self._pins).encode())
        self._lib.nova_map_follow(self._map, 1, 1 if (len(points) >= 2 and self._heading_up()) else 0, 16.0, 0.0, 0.0, 400.0)

    def _frame_preview(self) -> None:
        if not self._map or not self._lib:
            return
        self._lib.nova_map_follow(self._map, 0, 0, 15.0, 0.0, 0.0, 0.0)
        self._lib.nova_map_set_route(self._map, route_geojson(self._preview_route).encode())
        pins = self._fn("nova_map_set_pins")
        if pins is not None:
            pins(self._map, pins_geojson(self._pins).encode())
        spots = [(float(pin[0]), float(pin[1])) for pin in self._pins if pin is not None and len(pin) >= 2]
        for point in self._preview_route:
            lat = getattr(point, "lat", None)
            lon = getattr(point, "lon", None)
            if lat is None:
                if point is None or len(point) < 2:
                    continue
                lat, lon = point[0], point[1]
            spots.append((float(lat), float(lon)))
        box = frame_bounds(spots)
        fit = self._fn("nova_map_fit")
        camera = self._fn("nova_map_camera")
        if box is not None and fit is not None:
            fit(self._map, box[0], box[1], box[2], box[3], 48.0, 400.0)
        elif camera is not None:
            if spots:
                lat = sum(p[0] for p in spots) / len(spots)
                lon = sum(p[1] for p in spots) / len(spots)
            else:
                lat, lon, _heading = self._position()
            camera(self._map, lat, lon, 15.0 if spots else 14.0, 400.0)

    def _apply_pace(self) -> None:
        live = self._rendering()
        if self._map and self._lib:
            # Hot: keep the map on screen, just redraw it slowly. Hiding it made the panel blink.
            self._lib.nova_map_set_period(self._map, (HOT_PERIOD_MS if self._hot() else 33) if live else 0)
        if live != self._live:
            self._live = live
            self.engine.emit("map_live", live)


class NovaMapProvider(QQuickImageProvider):
    """`image://novamap/frame`. Each request copies the newest worker frame."""

    def __init__(self) -> None:
        global _provider
        super().__init__(QQuickImageProvider.ImageType.Image)
        self.session = _session
        self._blank = QImage(2, 2, QImage.Format.Format_ARGB32)
        self._blank.fill(0)
        self._last = None
        _provider = self

    def current(self) -> QImage:
        """The newest frame, or the previous one when the worker has nothing new."""
        frame = self.session.pull() if self.session is not None else None
        if frame is None:
            return self._last if self._last is not None else self._blank
        w, h, stride, data = frame
        img = QImage(data, w, h, stride, QImage.Format.Format_RGB32).copy()
        self._last = img
        return img

    def requestImage(self, _id: str, size: QSize, requested: QSize) -> QImage:
        img = self.current()
        if size is not None:
            size.setWidth(img.width())
            size.setHeight(img.height())
        return img


class MapFrameItem(QQuickPaintedItem):
    """Paints the map in place. A changing image:// URL was clearing the picture every frame."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setOpaquePainting(True)
        self.setAntialiasing(False)
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self.update)
        self._timer.start()

    def paint(self, painter: QPainter) -> None:
        if _provider is None or not self.isVisible():
            return
        img = _provider.current()
        target = self.boundingRect()
        if img.width() < 8 or target.width() <= 0 or target.height() <= 0:
            return
        scale = max(target.width() / img.width(), target.height() / img.height())
        dw, dh = img.width() * scale, img.height() * scale
        x = target.x() + (target.width() - dw) / 2
        y = target.y() + (target.height() - dh) / 2
        painter.setClipRect(target)
        painter.drawImage(QRectF(x, y, dw, dh), img)


qmlRegisterType(MapFrameItem, "NovaMap", 1, 0, "MapFrame")
