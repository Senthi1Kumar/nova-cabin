"""Chooses the payment verifier: DriveOS's real voice/face ladder, or its simulated one.

Both are driven through the same ``verify(timeout) -> AuthResult`` surface and the same DriveAuth decision
engine (voice, then face, then a one-time code). The simulated verifier is used under ``--sim`` and whenever
neither a usable voice path nor a face camera is present.
"""
from __future__ import annotations

import collections
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from .. import audio

log = logging.getLogger("payment.bridge")

OUTCOMES = ("accept", "impostor", "spoof", "unavailable")


class MicRing:
    """The engine microphone as DriveOS reads it: ``seq`` frames pushed so far, ``frames_since(seq)`` as
    ``(seq, pcm16 bytes, None)`` (16 kHz mono). Fed from ``audio.frame_listeners``; the audio thread only appends."""

    device_name = ""

    def __init__(self, seconds: float = 8.0):
        self.seq = 0
        self._ring: collections.deque = collections.deque(maxlen=max(1, int(seconds * 1000 / audio.FRAME_MS)))
        self._lock = threading.Lock()

    def push(self, frame: np.ndarray) -> None:
        with self._lock:
            self._ring.append((self.seq, np.array(frame, dtype=np.float32, copy=True)))
            self.seq += 1

    def frames_since(self, seq: int) -> list[tuple[int, bytes, bytes | None]]:
        with self._lock:
            items = [it for it in self._ring if it[0] >= seq]
        return [(n, (np.clip(x, -1.0, 1.0) * 32767).astype(np.int16).tobytes(), None) for n, x in items]

    def last_seconds(self, seconds: float) -> np.ndarray:
        n = max(1, int(seconds * 1000 / audio.FRAME_MS))
        with self._lock:
            items = list(self._ring)[-n:]
        return np.concatenate([x for _, x in items]) if items else np.zeros(0, dtype=np.float32)

    def attach(self) -> None:
        if self.push not in audio.frame_listeners:
            audio.frame_listeners.append(self.push)

    def detach(self) -> None:
        if self.push in audio.frame_listeners:
            audio.frame_listeners.remove(self.push)


class _SessionTranscriber:
    """Adapts the engine's session-style STT to DriveOS's ``transcribe(pcm16, vocabulary=)``."""

    def __init__(self, get_stt: Callable[[], Any]):
        self._get = get_stt

    async def transcribe(self, pcm16: bytes, *, vocabulary: str = "") -> str:
        stt = self._get()
        if stt is None:
            return ""
        s = stt.session(None)
        await s.feed(pcm16)
        return await s.finish()


class VerifierBridge:
    def __init__(self, home: Path, *, simulated: bool, sim_voice: str = "accept", sim_face: str = "accept",
                 step_s: float = 0.9, stt: Callable[[], Any] | None = None):
        self.home = home
        self._simulated_initial = simulated
        self.simulated = simulated
        self._get_stt = stt
        self.ring: MicRing | None = None
        self.sim_voice = sim_voice if sim_voice in OUTCOMES else "accept"
        self.sim_face = sim_face if sim_face in OUTCOMES else "accept"
        self.step_s = step_s
        self._sim: Any = None
        self._voice: Any = None
        self._face: Any = None
        self._built = False

    def reset(self) -> None:
        """Forget the verifiers built so far; the next ``verifier_for`` rebuilds them (after an enrollment)."""
        if self.ring is not None:
            self.ring.detach()
            self.ring = None
        self._built = False
        self._voice = self._face = self._sim = None
        self.simulated = self._simulated_initial

    # construction ------------------------------------------------------------
    def _config(self):
        from driveos.config import BiometricsConfig
        return BiometricsConfig(home=self.home / "driveos", sim_voice=self.sim_voice, sim_face=self.sim_face)

    def _build(self) -> None:
        if self._built:
            return
        self._built = True
        if not self.simulated:
            cfg = self._config()
            try:
                from driveos.biometrics.factory import (
                    build_face_first,
                    build_voice_first,
                )
                voice, why = build_voice_first(cfg, require_enrollment=True)
                if voice is not None:
                    self.ring = MicRing()
                    self.ring.attach()
                    voice.attach_audio(self.ring, _SessionTranscriber(self._get_stt or (lambda: None)))
                face = None
                if voice is not None:
                    from driveos.biometrics.verifier import FaceFirstVerifier
                    face = FaceFirstVerifier.sharing(voice)
                else:
                    face = build_face_first(cfg)
                self._voice, self._face = voice, face
                if voice is None and face is None:
                    log.info("no usable voice or camera verifier (%s); using the simulated ladder", why)
            except Exception as e:                                   # noqa: BLE001
                log.info("real verifier unavailable (%s); using the simulated ladder", type(e).__name__)
            if self._voice is None and self.ring is not None:
                self.ring.detach()
                self.ring = None
            if self._voice is None and self._face is None:
                self.simulated = True
        if self.simulated:
            from driveos.biometrics.sim import SimulatedVoiceFirstVerifier
            self._sim = SimulatedVoiceFirstVerifier(self._config(), step_s=self.step_s)

    def real_methods(self) -> list[str]:
        """Which real verifiers exist ("Voice ID", "Face ID"); builds them on first call."""
        self._build()
        if self.simulated:
            return []
        return [n for n, v in (("Voice ID", self._voice), ("Face ID", self._face)) if v is not None]

    # use ---------------------------------------------------------------------
    def verifier_for(self, method: str, host) -> Any | None:
        """The verifier for ``voice`` or ``face``; None when that modality is not available on this device."""
        self._build()
        if self.simulated:
            sim = self._sim
            sim.bind(host)
            if method == "face":
                sim.set_outcome("voice", "unavailable")
            else:
                sim.set_outcome("voice", self.sim_voice)
            sim.set_outcome("face", self.sim_face)
            return sim
        chosen = self._voice if method == "voice" else self._face
        if chosen is not None:
            chosen.bind(host)
        return chosen

