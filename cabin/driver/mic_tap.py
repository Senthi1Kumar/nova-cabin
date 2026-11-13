"""A tap on the engine's microphone for enrollment takes.

The engine's ``Microphone`` calls ``mic.on_frame`` for every 20 ms frame. While a take is being recorded the tap
keeps the frames for itself and does not pass them on, so the enrollment phrase never starts an assistant turn.
Otherwise frames flow to the engine untouched. The tap is also the ring source DriveOS's ``capture_utterance`` reads.
"""
from __future__ import annotations

import collections
import contextlib
import threading

import numpy as np

RING_FRAMES = 600          # 12 s of 20 ms frames


class MicTap:
    def __init__(self, ring_frames: int = RING_FRAMES):
        self.seq = 0
        self.recording = False
        self._ring: collections.deque = collections.deque(maxlen=ring_frames)
        self._lock = threading.Lock()
        self._mic = None
        self._original = None

    def attach(self, mic) -> bool:
        """Wrap ``mic.on_frame``. False when there is no working microphone."""
        if mic is None or not getattr(mic, "available", False) or getattr(mic, "on_frame", None) is None:
            return False
        if self._mic is mic:
            return True
        self.detach()
        original = mic.on_frame

        def tapped(frame) -> None:
            if self.recording:
                self.push(frame)
                return
            original(frame)

        self._mic, self._original = mic, original
        mic.on_frame = tapped
        return True

    def detach(self) -> None:
        if self._mic is not None and self._original is not None:
            self._mic.on_frame = self._original
        self._mic = self._original = None

    def push(self, frame) -> None:
        pcm = (np.clip(np.asarray(frame, dtype=np.float32), -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        with self._lock:
            self._ring.append((self.seq, pcm, None))
            self.seq += 1

    def frames_since(self, seq: int) -> list[tuple[int, bytes, bytes | None]]:
        with self._lock:
            return [item for item in self._ring if item[0] >= seq]

    @contextlib.contextmanager
    def capturing(self):
        with self._lock:
            self._ring.clear()
        self.recording = True
        try:
            yield self
        finally:
            self.recording = False
