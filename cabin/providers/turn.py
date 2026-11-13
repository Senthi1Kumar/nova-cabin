"""Turn-taking with Silero VAD and Smart Turn (DriveOS implementations).

``SileroTurn`` is driven by ``cabin.audio.VAD.update`` once per 20 ms frame and returns the same
``(event, level)`` the energy VAD does. Silero detects speech and short pauses; once a pause of
``min_silence_ms`` (240 ms) is seen, Smart Turn decides whether the speaker is done. An undecided
pause resumes the utterance and the fixed ``end_silence_ms`` stays the upper bound.

While the assistant is audible the energy VAD's echo-aware decision is used instead, since Silero
would otherwise fire on playback bleed.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

log = logging.getLogger("turn")

MIN_SILENCE_MS = 240
FRAME_MS = 20


def _paths() -> tuple[Path, Path]:
    from driveos.voice.silero_vad import DEFAULT_ONNX as silero
    from driveos.voice.smart_turn import DEFAULT_ONNX as smart
    return (Path(os.path.expanduser(os.environ.get("SILERO_MODEL") or silero)),
            Path(os.path.expanduser(os.environ.get("SMART_TURN_MODEL") or smart)))


def models_present() -> bool:
    try:
        return _paths()[0].is_file()
    except Exception:                                   # noqa: BLE001
        return False


class SileroTurn:
    def __init__(self, cfg, energy, *, segmenter=None, analyzer=None, min_silence_ms: int | None = None) -> None:
        self.cfg = cfg
        self.energy = energy
        silero_path, smart_path = _paths() if segmenter is None or analyzer is None else (None, None)
        self.min_silence_ms = int(min_silence_ms or os.environ.get("VAD_MIN_SILENCE_MS") or MIN_SILENCE_MS)
        if analyzer is None:
            try:
                from driveos.voice.smart_turn import SmartTurnAnalyzer
                analyzer = SmartTurnAnalyzer(smart_path)
            except Exception as e:                      # noqa: BLE001
                log.warning("Smart Turn unavailable, every pause ends after %d ms: %s", cfg.end_silence_ms, e)
        self.analyzer = analyzer
        if analyzer is not None and segmenter is None:
            try:
                analyzer.probability(np.zeros(8000, np.float32))     # first inference is slow; pay it now
            except Exception as e:                      # noqa: BLE001
                log.warning("Smart Turn warm-up failed: %s", e)
        if segmenter is None:
            from driveos.voice.silero_vad import SileroVadConfig, SileroVadSegmenter
            segmenter = SileroVadSegmenter(SileroVadConfig(
                threshold=0.55 - 0.15 * (cfg.sensitivity - 0.6),
                min_silence_ms=self.min_silence_ms if analyzer else cfg.end_silence_ms,
                min_speech_ms=max(cfg.min_speech_ms // 2, 100),
                speech_pad_ms=100,
                max_utterance_s=cfg.max_utterance_s + 10,
                onnx_path=silero_path))
        self.seg = segmenter
        self.last_turn_p = 0.0
        self.reset()

    def reset(self) -> None:
        self.in_speech = False
        self.run_on = 0
        self.run_off = 0
        self._playing = False
        self.seg.reset()

    def _end(self) -> str:
        self.in_speech = False
        self.run_on = self.run_off = 0
        self.seg.reset()
        return "end"

    def update(self, frame: np.ndarray, playback_db: float | None, energy_ev: str | None,
               level: float) -> tuple[str | None, float]:
        if playback_db is not None and playback_db > -60:
            if not self._playing:
                self.seg.reset()
                self.in_speech = False
                self._playing = True
            self.run_on = self.run_off = 0
            return energy_ev, level
        self._playing = False

        pcm = (np.clip(frame, -1, 1) * 32767).astype(np.int16).tobytes()
        events = self.seg.feed(pcm)
        voiced = self.seg.last_prob >= self.seg.cfg.threshold
        if voiced:
            self.run_on += FRAME_MS
            self.run_off = 0
        else:
            self.run_off += FRAME_MS
            self.run_on = 0

        if not self.in_speech:
            if self.run_on >= self.cfg.start_ms:
                self.in_speech = True
                return "start", level
            return None, level

        for name, audio in events:
            if name == "pause" and audio is not None:
                if self.analyzer is None:
                    return self._end(), level
                pcm_utt = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
                p = self.analyzer.probability(audio) if len(pcm_utt) else 0.0
                self.last_turn_p = p
                if p > 0.5:
                    return self._end(), level
                self.seg.resume()
            elif name == "speech_stopped":
                return self._end(), level
        if self.run_off >= self.cfg.end_silence_ms:
            return self._end(), level
        return None, level
