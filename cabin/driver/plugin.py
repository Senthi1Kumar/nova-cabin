"""Installs the driver sheet on the engine."""
from __future__ import annotations

from .sheet import DriverSheet


def install(engine) -> None:
    engine.driver_sheet = DriverSheet(engine)
