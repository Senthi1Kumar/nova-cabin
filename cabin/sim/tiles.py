"""Procedural dark map tiles: a believable street network, parks, the Pacific
coast, SF Bay and Lake Tahoe — generated deterministically in world space so
tiles join seamlessly at any zoom. Used when simulation runs offline."""
from __future__ import annotations

import hashlib
import io
import math

from PIL import Image, ImageDraw

BG = (15, 19, 33)
WATER = (10, 19, 42)
SHORE = (30, 46, 88)
PARK = (18, 33, 38)
ROAD = {"minor": ((31, 39, 62), 2), "art": ((46, 57, 92), 4), "hwy": ((64, 76, 124), 6), "fwy": ((82, 96, 150), 8)}
REF_Z = 16

PACIFIC = [(41.0, -124.3), (39.5, -123.8), (38.95, -123.73), (38.3, -123.05), (38.0, -122.98), (37.9, -122.68),
           (37.81, -122.52), (37.78, -122.51), (37.6, -122.50), (37.45, -122.44), (37.2, -122.41),
           (36.97, -122.15), (36.95, -121.98), (36.85, -121.80), (36.60, -121.89), (36.52, -121.94),
           (36.2, -121.75), (35.6, -121.15), (35.2, -120.88), (34.45, -120.45), (34.40, -119.7), (34.0, -118.5),
           (33.7, -118.3), (32.5, -117.1), (30.0, -130.0), (41.0, -130.0)]
BAY = [(37.815, -122.478), (37.86, -122.45), (37.89, -122.44), (37.95, -122.43), (38.03, -122.36),
       (38.07, -122.25), (38.03, -122.14), (37.97, -122.33), (37.90, -122.32), (37.82, -122.33),
       (37.80, -122.29), (37.72, -122.21), (37.60, -122.14), (37.48, -122.03), (37.44, -122.05),
       (37.49, -122.15), (37.58, -122.29), (37.67, -122.37), (37.73, -122.38), (37.78, -122.39),
       (37.81, -122.405), (37.808, -122.45)]
TAHOE = [(39.25, -120.00), (39.24, -119.95), (39.17, -119.93), (39.08, -119.94), (38.97, -119.95),
         (38.94, -120.01), (38.96, -120.09), (39.04, -120.14), (39.12, -120.16), (39.21, -120.10)]
WATERS = [PACIFIC, BAY, TAHOE]


def _h(*p) -> float:
    return int(hashlib.md5(repr(p).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def _px(lat, lon, z):
    s = 256 * 2 ** z
    x = (lon + 180) / 360 * s
    siny = min(max(math.sin(math.radians(lat)), -0.9999), 0.9999)
    return x, (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * s


def render_tile(z: int, tx: int, ty: int) -> bytes:
    S = 2
    img = Image.new("RGB", (256 * S, 256 * S), BG)
    d = ImageDraw.Draw(img)
    scale = 2 ** (REF_Z - z)                     # ref px per tile px
    X0, Y0 = tx * 256 * scale, ty * 256 * scale  # tile origin in ref space
    span = 256 * scale

    def to_tile(xr, yr):
        return ((xr - X0) / scale * S, (yr - Y0) / scale * S)

    levels = [("hwy", 16000, 9), ("art", 1300, 11), ("minor", 230, 14)]
    for kind, sp, zmin in reversed(levels):
        if z < zmin:
            continue
        col, w = ROAD[kind]
        w = max(1, int(w * (1.0 if z >= zmin + 2 else 0.7)))
        wob = sp * 0.18
        for axis in (0, 1):
            a0, a1 = (X0, X0 + span) if axis == 0 else (Y0, Y0 + span)
            for k in range(int(a0 // sp) - 1, int(a1 // sp) + 2):
                if kind == "minor" and _h("gap", axis, k) < 0.12:
                    continue
                base = k * sp + (_h(kind, axis, k) - 0.5) * sp * 0.35
                pts = []
                steps = 12
                for i in range(steps + 1):
                    t = (Y0 if axis == 0 else X0) - span * 0.05 + span * 1.1 * i / steps
                    off = wob * math.sin(t / (sp * 3.1) + k * 1.7) * (0.25 if kind == "minor" else 1)
                    pts.append(to_tile(base + off, t) if axis == 0 else to_tile(t, base + off))
                d.line(pts, fill=col, width=w, joint="curve")

    if z >= 12:                                   # parks in some arterial blocks
        sp = 1300
        for i in range(int(X0 // sp) - 1, int((X0 + span) // sp) + 1):
            for j in range(int(Y0 // sp) - 1, int((Y0 + span) // sp) + 1):
                if _h("park", i, j) < 0.10:
                    x, y = to_tile(i * sp + sp * 0.25, j * sp + sp * 0.25)
                    x2, y2 = to_tile(i * sp + sp * 0.75, j * sp + sp * 0.72)
                    d.rounded_rectangle((x, y, x2, y2), radius=int(10 * S), fill=PARK)

    from .roads import EDGES, edge_points
    col, w = ROAD["fwy"]
    for e in EDGES:                               # real highway corridors, all zooms
        pts = []
        for lat, lon in edge_points(e):
            wx, wy = _px(lat, lon, z)
            pts.append(((wx - tx * 256) * S, (wy - ty * 256) * S))
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        if max(xs) < -50 or min(xs) > 256 * S + 50 or max(ys) < -50 or min(ys) > 256 * S + 50:
            continue
        d.line(pts, fill=col, width=w if z >= 9 else 5, joint="curve")

    for poly in WATERS:
        pts = []
        for lat, lon in poly:
            wx, wy = _px(lat, lon, z)
            pts.append(((wx - tx * 256) * S, (wy - ty * 256) * S))
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        if max(xs) < 0 or min(xs) > 256 * S or max(ys) < 0 or min(ys) > 256 * S:
            continue
        d.polygon(pts, fill=WATER, outline=SHORE, width=2)

    b = io.BytesIO()
    img.save(b, "PNG", optimize=False, compress_level=3)
    return b.getvalue()
