"""Qt side of the Diagnostics panel: a context-property object fed by the engine's ``diagnostics`` events."""
from __future__ import annotations

import time

from PySide6.QtCore import Property, QObject, Signal, Slot


class DiagnosticsBridge(QObject):
    changed = Signal()

    def __init__(self, engine=None) -> None:
        super().__init__()
        self._info: dict = {}
        self._engine = engine

    def _get(self) -> dict:
        return self._info

    info = Property("QVariantMap", _get, notify=changed)

    @Slot(float)
    def reportFps(self, fps: float) -> None:
        if self._engine is not None:
            self._engine.ui_fps, self._engine.ui_fps_ts = float(fps), time.monotonic()

    @Slot(str, "QVariant")
    def apply(self, key: str, value) -> None:
        if key == "diagnostics" and isinstance(value, dict):
            self._info = value
            self.changed.emit()


def make(runtime=None) -> DiagnosticsBridge:
    """Create the bridge and connect it to the runtime; it lives as long as the runtime (or the process)."""
    bridge = DiagnosticsBridge(getattr(runtime, "engine", None))
    if runtime is not None:
        runtime.bridge.update.connect(bridge.apply)
        runtime.diagnostics = bridge
    else:
        _keep.append(bridge)
    return bridge


_keep: list[DiagnosticsBridge] = []
