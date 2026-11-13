"""Coarse highway network (California + West Coast) for offline routing and tiles.
Polylines are approximate corridors (lat, lon) — good enough that routes follow
I-80 over Donner, US-50 to Tahoe, I-5 down the valley, etc."""
from __future__ import annotations

import heapq

from ..services.maps import haversine_m

N = {  # junction nodes
    "SF": (37.7749, -122.4194), "OAK": (37.8044, -122.2712), "VAL": (38.1041, -122.2566),
    "VAC": (38.3566, -121.9877), "SAC": (38.5816, -121.4944), "AUB": (38.8966, -121.0769),
    "TRK": (39.3280, -120.1833), "RNO": (39.5296, -119.8138), "TAH": (38.9399, -119.9772),
    "PLV": (38.7296, -120.7985), "SAU": (37.8591, -122.4853), "NAP": (38.2975, -122.2869),
    "SON": (38.2919, -122.4580), "PA": (37.4419, -122.1430), "SJ": (37.3382, -121.8863),
    "GIL": (37.0058, -121.5683), "SAL": (36.6777, -121.6555), "MRY": (36.6002, -121.8947),
    "BSR": (36.2704, -121.8081), "SCZ": (36.9741, -122.0308), "HMB": (37.4636, -122.4286),
    "LIV": (37.6819, -121.7680), "TRA": (37.7397, -121.4252), "MOD": (37.6391, -120.9969),
    "MER": (37.3022, -120.4830), "FRE": (36.7378, -119.7871), "YOS": (37.7456, -119.5936),
    "KET": (36.0083, -119.9618), "LA": (34.0522, -118.2437), "SLO": (35.2828, -120.6596),
    "SB": (34.4208, -119.6982), "LV": (36.1699, -115.1398), "BAR": (34.8958, -117.0173),
    "RED": (40.5865, -122.3917), "EUG": (44.0521, -123.0868), "PDX": (45.5152, -122.6784),
    "SEA": (47.6062, -122.3321), "PRY": (38.0690, -122.8069), "MUIR": (37.8970, -122.5811),
}

EDGES = [  # (from, to, name, intermediate points)
    ("SF", "OAK", "I-80", [(37.7983, -122.3778)]),
    ("OAK", "VAL", "I-80", [(37.90, -122.30), (38.00, -122.26)]),
    ("VAL", "VAC", "I-80", [(38.2494, -122.04)]),
    ("VAC", "SAC", "I-80", [(38.5449, -121.7405)]),
    ("SAC", "AUB", "I-80", [(38.75, -121.29)]),
    ("AUB", "TRK", "I-80", [(39.10, -120.95), (39.31, -120.55), (39.32, -120.33)]),
    ("TRK", "RNO", "I-80", [(39.42, -120.03), (39.50, -119.90)]),
    ("SAC", "PLV", "US-50", [(38.68, -121.17)]),
    ("PLV", "TAH", "US-50", [(38.79, -120.43), (38.80, -120.10), (38.88, -120.00)]),
    ("TRK", "TAH", "CA-89", [(39.17, -120.14), (39.02, -120.12)]),
    ("SF", "SAU", "US-101", [(37.80, -122.44), (37.8199, -122.4783)]),
    ("SAU", "SON", "US-101", [(37.97, -122.53), (38.10, -122.57), (38.22, -122.52)]),
    ("SON", "NAP", "CA-12", [(38.27, -122.37)]),
    ("VAL", "NAP", "CA-29", [(38.20, -122.27)]),
    ("SAU", "PRY", "CA-1", [(37.97, -122.62)]),
    ("SAU", "MUIR", "CA-1", [(37.88, -122.53)]),
    ("SF", "PA", "US-101", [(37.70, -122.40), (37.6213, -122.3790), (37.55, -122.30)]),
    ("PA", "SJ", "US-101", [(37.39, -122.07)]),
    ("SJ", "GIL", "US-101", [(37.20, -121.70)]),
    ("GIL", "SAL", "US-101", [(36.85, -121.55)]),
    ("SAL", "MRY", "CA-68", [(36.62, -121.78)]),
    ("MRY", "BSR", "CA-1", [(36.5552, -121.9233), (36.40, -121.90)]),
    ("SJ", "SCZ", "CA-17", [(37.23, -121.98), (37.10, -121.98)]),
    ("SCZ", "MRY", "CA-1", [(36.91, -121.80), (36.80, -121.78)]),
    ("SF", "HMB", "CA-1", [(37.65, -122.47)]),
    ("OAK", "LIV", "I-580", [(37.70, -122.08)]),
    ("LIV", "TRA", "I-580", [(37.73, -121.55)]),
    ("TRA", "MOD", "CA-120", [(37.65, -121.15)]),
    ("MOD", "MER", "CA-99", [(37.47, -120.75)]),
    ("MER", "FRE", "CA-99", [(37.00, -120.10)]),
    ("MER", "YOS", "CA-140", [(37.48, -120.00), (37.60, -119.85)]),
    ("TRA", "KET", "I-5", [(37.00, -120.90), (36.50, -120.40)]),
    ("KET", "LA", "I-5", [(35.40, -119.40), (34.95, -118.95), (34.40, -118.60)]),
    ("SAL", "SLO", "US-101", [(36.20, -121.10), (35.60, -120.70)]),
    ("SLO", "SB", "US-101", [(34.95, -120.43), (34.60, -120.20)]),
    ("SB", "LA", "US-101", [(34.27, -119.25), (34.15, -118.80)]),
    ("LA", "BAR", "I-15", [(34.06, -117.60), (34.50, -117.30)]),
    ("BAR", "LV", "I-15", [(35.30, -116.10), (35.60, -115.40)]),
    ("SAC", "RED", "I-5", [(39.50, -122.20)]),
    ("RED", "EUG", "I-5", [(41.70, -122.60), (42.33, -122.87), (43.20, -123.35)]),
    ("EUG", "PDX", "I-5", [(44.94, -123.04)]),
    ("PDX", "SEA", "I-5", [(46.60, -122.90)]),
]


def edge_points(e) -> list[tuple[float, float]]:
    a, b, _name, mid = e
    return [N[a]] + mid + [N[b]]


def _length(pts) -> float:
    return sum(haversine_m(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


_ADJ: dict[str, list[tuple[str, float, list]]] = {}
for _e in EDGES:
    _p = edge_points(_e)
    _L = _length(_p)
    _ADJ.setdefault(_e[0], []).append((_e[1], _L, _p))
    _ADJ.setdefault(_e[1], []).append((_e[0], _L, _p[::-1]))


def nearest_node(p: tuple[float, float]) -> tuple[str, float]:
    k = min(N, key=lambda n: haversine_m(p, N[n]))
    return k, haversine_m(p, N[k])


def route_polyline(a: tuple[float, float], b: tuple[float, float]) -> list[tuple[float, float]] | None:
    """Highway path a → b, or None when a/b aren't near the network or it's a local trip."""
    if haversine_m(a, b) < 30_000:
        return None
    na, da = nearest_node(a)
    nb, db = nearest_node(b)
    if da > 70_000 or db > 70_000 or na == nb:
        return None
    dist = {na: 0.0}
    prev: dict[str, tuple[str, list]] = {}
    q = [(0.0, na)]
    while q:
        d, n = heapq.heappop(q)
        if n == nb:
            break
        if d > dist.get(n, 1e18):
            continue
        for m, L, pts in _ADJ.get(n, []):
            nd = d + L
            if nd < dist.get(m, 1e18):
                dist[m] = nd
                prev[m] = (n, pts)
                heapq.heappush(q, (nd, m))
    if nb not in prev:
        return None
    chain = []
    n = nb
    while n != na:
        p, pts = prev[n]
        chain.append(pts)
        n = p
    out = [a]
    for pts in reversed(chain):
        out.extend(pts)
    out.append(b)
    return out
