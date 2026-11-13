"""Hosts the Engine on a dedicated asyncio thread and bridges it to Qt."""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .config import Settings
from .engine import Engine

log = logging.getLogger("runtime")


class Bridge(QObject):
    """Created on the GUI thread; emitting from the engine thread is queued."""
    update = Signal(str, object)


class Runtime:
    def __init__(self, cfg: Settings, cache: Path, sim: bool = False, autopilot: bool = False) -> None:
        self.bridge = Bridge()
        self.engine = Engine(cfg, self.bridge.update.emit, cache, sim=sim, autopilot=autopilot)
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name="engine", daemon=True)

    def start(self) -> None:
        self.thread.start()
        self._ready.wait(10)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)

        async def boot():
            try:
                await self.engine.start()
            except Exception as e:                       # noqa: BLE001
                log.exception("engine start failed")
                self.bridge.update.emit("toast", f"Startup error: {e}")
            finally:
                self._ready.set()
        self.loop.create_task(boot())
        self.loop.run_forever()

    def call(self, fn, *args) -> None:
        """Run a sync engine method on the engine loop."""
        self.loop.call_soon_threadsafe(fn, *args)

    def shutdown(self) -> None:
        fut = asyncio.run_coroutine_threadsafe(self.engine.stop(), self.loop)
        try:
            fut.result(3)
        except Exception:                                # noqa: BLE001
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
