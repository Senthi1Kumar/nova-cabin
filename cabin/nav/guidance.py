"""Cue, off-route and reroute policy. The trackers themselves come from DriveOS."""
from __future__ import annotations

from driveos.domain.models import GeoPoint
from driveos.nav.geo import project
from driveos.nav.guidance import (
    CueTracker,
    EtaRefresh,
    OffRouteDetector,
    RerouteGate,
    lanes_at,
)

__all__ = ["CueTracker", "EtaRefresh", "OffRouteDetector", "RerouteGate", "lanes_at", "project_fix"]


def project_fix(route, lat: float, lon: float):
    return project(route, GeoPoint(lat=lat, lon=lon))
