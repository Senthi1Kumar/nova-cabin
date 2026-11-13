"""Engine seam: exposes ``engine.telemetry``, runs the resource sampler, flushes on shutdown."""
from __future__ import annotations

import asyncio

from . import install as _install
from .sampler import Sampler


def install(engine) -> None:
    cfg = getattr(engine, "cfg", None)
    tel = _install(cfg)
    engine.telemetry = tel
    closers = getattr(engine, "closers", None)

    sampler = None
    if tel.enabled and (cfg.get("TELEMETRY_SAMPLER", "on") if cfg is not None else "on").lower() not in (
            "off", "0", "false", "no"):
        try:
            interval = float(cfg.get("TELEMETRY_SAMPLE_S", "5") or 5) if cfg is not None else 5.0
        except ValueError:
            interval = 5.0
        sampler = Sampler(interval, engine=engine, telemetry=tel,
                          emit=lambda k, v: engine.emit(k, v))
        sampler.start()

    async def close() -> None:
        if sampler is not None:
            await asyncio.to_thread(sampler.stop)
        tel.close()

    if closers is not None:
        closers.append(close)
