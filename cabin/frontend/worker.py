"""Front-end worker: mic frames go through an ordered list of stages on one daemon thread.

``push`` is called from the audio thread (or the event loop) and never blocks; when the bounded
queue is full the oldest frame is dropped and counted. Each stage is ``process(frame, ctx)`` and
returns the (possibly modified) frame, or ``None`` to drop it for the stages after it. Stages
report what they detected through ``ctx.emit``; events reach ``on_event`` on the worker thread,
in the order they were produced.
"""
from __future__ import annotations

import collections
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

import numpy as np

log = logging.getLogger("frontend")

QUEUE_FRAMES = 50

Kind = Literal["start", "end", "level", "wake"]


@dataclass
class FrontEvent:
    kind: Kind
    t: float
    level: float
    text: str = ""
    seq: int = 0
    onset_t: float = 0.0        # on a late wake "start": capture time of the frame the VAD started on


@dataclass
class Context:
    playback_db: float | None
    t: float
    stats: dict
    seq: int = 0
    events: list[FrontEvent] = field(default_factory=list)

    def emit(self, kind: Kind, level: float = 0.0, text: str = "") -> None:
        self.events.append(FrontEvent(kind, self.t, level, text, self.seq))


class Stage(Protocol):
    """A stage may also define ``reset()``; it is called on the worker thread when present."""

    def process(self, frame: np.ndarray, ctx: Context) -> np.ndarray | None: ...


class FrontEnd:
    def __init__(self, cfg, on_event: Callable[[FrontEvent], None], reference=None, *,
                 stages: list[Stage] | None = None) -> None:
        self.cfg = cfg
        self.on_event = on_event
        self.reference = reference
        self.stages: list[Stage] = list(stages or [])
        self.stats: dict = {"frames": 0, "dropped": 0, "errors": 0}
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_FRAMES)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._reset = threading.Event()
        self._push_lock = threading.Lock()
        self._seq = 0
        self.reset_seq = 0          # frames and events with a lower seq predate the last reset
        self.clean_frames: collections.deque = collections.deque(maxlen=64)   # echo-cancelled mic frames
        from .aec import install
        install(self)               # echo cancellation + residual gate in front of the VAD stage
        from .gate import install_gate
        install_gate(self)          # driver zone gate in front of the VAD stage, wake phrase behind it

    @property
    def mode(self) -> str:
        """``full_duplex``, or ``half_duplex`` once the echo canceller has given up."""
        return self.stats.get("mode", "full_duplex")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="frontend", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=2.0)
        for s in self.stages:
            close = getattr(s, "close", None)
            if close is not None:
                close()

    def push(self, frame: np.ndarray, playback_db: float | None = None, t: float | None = None) -> None:
        """``t`` is when the frame was captured (monotonic); frames from `Microphone` carry it."""
        if t is None:
            t = getattr(frame, "t", None) or time.monotonic()
        frame = np.asarray(frame)
        with self._push_lock:
            self._seq += 1
            item = (frame, playback_db, t, self._seq)
            try:
                self._q.put_nowait(item)
            except queue.Full:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    pass
                self.stats["dropped"] += 1
                try:
                    self._q.put_nowait(item)
                except queue.Full:
                    self.stats["dropped"] += 1

    def reset(self) -> None:
        """Discard everything queued so far and have the worker reset every stage before its next
        frame. Events from frames pushed before this call carry a seq below ``reset_seq``."""
        with self._push_lock:
            self.reset_seq = self._seq + 1
        self._reset.set()

    def _reset_stages(self) -> None:
        for s in self.stages:
            fn = getattr(s, "reset", None)
            if fn is not None:
                try:
                    fn()
                except Exception:
                    log.exception("stage reset failed")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                frame, playback_db, t, seq = self._q.get(timeout=0.1)
            except queue.Empty:
                if self._reset.is_set():
                    self._reset.clear()
                    self._reset_stages()
                continue
            if self._reset.is_set():
                self._reset.clear()
                self._reset_stages()
            if seq < self.reset_seq:
                continue
            self.process(frame, playback_db, t, seq)

    def process(self, frame: np.ndarray, playback_db: float | None, t: float, seq: int = 0) -> None:
        ctx = Context(playback_db, t, self.stats, seq)
        x: np.ndarray | None = frame
        for s in self.stages:
            try:
                x = s.process(x, ctx)
            except Exception:
                self.stats["errors"] += 1
                log.exception("front-end stage %s failed", type(s).__name__)
                break
            if x is None:
                break
        self.stats["frames"] += 1
        for ev in ctx.events:
            try:
                self.on_event(ev)
            except Exception:
                log.exception("front-end event handler failed")
