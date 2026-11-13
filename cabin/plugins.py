"""Optional feature modules plugged into the Engine at start-up.

Each module exposes ``install(engine)`` and registers through the extension points:
``engine.tools.extend(...)``, ``engine.action_handlers[key]``, ``engine.intercepts``,
``engine.system_extra``. A module that is absent or fails to import is skipped with a log line,
so the base assistant always starts.
"""
from __future__ import annotations

import importlib
import logging
import os

log = logging.getLogger("plugins")

MODULES = (
    "cabin.commerce",
    "cabin.payment",
    "cabin.vehicle_tools",
    "cabin.thermal",
    "cabin.frontend.plugin",
    "cabin.nav.plugin",
    "cabin.memory.plugin",
    "cabin.telemetry.plugin",
    "cabin.driver.plugin",
    "cabin.agents.plugin",
)


def _disabled(engine) -> set[str]:
    raw = os.environ.get("NOVA_PLUGINS_OFF", "")
    cfg = getattr(engine, "cfg", None)
    if cfg is not None:
        raw = f"{raw},{cfg.get('NOVA_PLUGINS_OFF') or ''}"
    return {t.strip() for t in raw.split(",") if t.strip()}


def _is_off(name: str, off: set[str]) -> bool:
    short = name.removeprefix("cabin.")
    return bool(off & {name, short, short.split(".")[0]})


def load(engine) -> list[str]:
    loaded: list[str] = []
    off = _disabled(engine)
    for name in MODULES:
        if _is_off(name, off):
            log.info("plugin %s disabled", name)
            continue
        try:
            mod = importlib.import_module(name)
        except ImportError as e:
            log.info("%s not available: %s", name, e)
            continue
        try:
            mod.install(engine)
            loaded.append(name)
        except Exception:
            log.exception("%s failed to install", name)
    return loaded
