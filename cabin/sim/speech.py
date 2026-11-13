"""Simulated speech: STT that turns real mic speech into scripted requests,
and a synthetic 'murmur' voice for machines without a system TTS."""
from __future__ import annotations

import asyncio
import math

import numpy as np

from ..audio import MIC_SR, TTS_SR
from ..tts import Speech, estimate_words


class SimSTT:
    """Your real voice drives VAD/endpointing; the transcript is the next scripted request,
    revealed word by word as you speak (≈3 words per second of audio)."""
    label = "Simulated STT"

    def __init__(self, script: list[str]) -> None:
        self.script = script
        self.i = 0
        self.override: str | None = None      # autopilot / tests can set the next transcript

    def session(self, on_partial=None):
        text = self.override or self.script[self.i % len(self.script)]
        if self.override is None:
            self.i += 1
        self.override = None
        return _Session(text, on_partial)


class _Session:
    def __init__(self, text: str, on_partial) -> None:
        self.words = text.split()
        self.on_partial = on_partial
        self.samples = 0
        self.shown = 0

    async def feed(self, pcm: bytes) -> None:
        self.samples += len(pcm) // 2
        n = min(len(self.words), int(self.samples / MIC_SR * 3.0))
        if n > self.shown and self.on_partial:
            self.shown = n
            r = self.on_partial(" ".join(self.words[:n]))
            if asyncio.iscoroutine(r):
                await r

    async def finish(self) -> str:
        await asyncio.sleep(0.15)
        return " ".join(self.words) if self.samples > MIC_SR * 0.25 else ""

    async def cancel(self) -> None:
        pass


class MurmurTTS:
    """Voice-like synthetic audio (glottal buzz + moving formants, syllabic envelope)."""
    label = "Synthetic voice"

    async def synth(self, text: str) -> Speech:
        words = estimate_words(text, max(0.6, len(text.split()) / 2.7))
        dur = (words[-1][1] + words[-1][2] + 0.12) if words else 0.6
        await asyncio.sleep(0.08)
        sr = TTS_SR
        t = np.arange(int(sr * dur)) / sr
        env = np.zeros_like(t)
        for _w, s, d in words:
            n_syl = max(1, round(d * 4.4))
            for k in range(n_syl):
                c = s + d * (k + 0.5) / n_syl
                env += np.exp(-((t - c) / (d / n_syl * 0.42)) ** 2)
        env = np.clip(env, 0, 1)
        f0 = 205 + 22 * np.sin(2 * math.pi * 0.7 * t) - 30 * (t / max(dur, 1e-3))
        phase = 2 * math.pi * np.cumsum(f0) / sr
        src = sum(np.sin(k * phase) / k for k in range(1, 14))
        f1 = 650 + 250 * np.sin(2 * math.pi * 3.1 * t)
        f2 = 1700 + 500 * np.sin(2 * math.pi * 2.3 * t + 1)
        voice = src * (0.6 + 0.4 * np.sin(2 * math.pi * f1 / sr * np.arange(len(t)))) \
                    * (0.7 + 0.3 * np.sin(2 * math.pi * f2 / sr * np.arange(len(t))))
        x = 0.16 * env * voice / (np.max(np.abs(voice)) + 1e-6)
        return Speech((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes(), words)
