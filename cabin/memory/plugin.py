"""Plugin entry: persisted sessions, compaction and the driver profile."""
from __future__ import annotations

import logging

log = logging.getLogger("memory.plugin")


def install(engine) -> None:
    if engine.sim or not engine.cfg.flag("MEMORY", True):
        return
    from . import Memory
    try:
        engine.memory = Memory(
            engine, driver_id=engine.cfg.get("DRIVER_ID", "driver"),
            budget_tokens=int(engine.cfg.num("MEMORY_BUDGET_TOKENS", 3500)),
            resume_s=engine.cfg.num("MEMORY_RESUME_HOURS", 4) * 3600, defer=True)
    except Exception:
        log.exception("memory unavailable; continuing without it")
        engine.memory = None
