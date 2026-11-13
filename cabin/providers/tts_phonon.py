"""On-device Phonon text-to-speech (ptts) behind the cabin `synth(text) -> Speech` interface."""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np

from ..audio import TTS_SR, resample, to_pcm16
from ..tts import Speech, estimate_words, split_clauses

log = logging.getLogger("tts.phonon")

MODEL_DIR = "models--gradium--phonon-31m-en"


def model_cached() -> bool:
    """True when ptts is importable and the Phonon weights are already in the Hugging Face cache."""
    try:
        import ptts  # noqa: F401
        from driveos.voice.tts import phonon_cache_dir
    except Exception:                                    # noqa: BLE001
        return False
    snaps = Path(phonon_cache_dir()) / MODEL_DIR / "snapshots"
    return snaps.is_dir() and any(snaps.iterdir())


class PhononTTS:
    label = "Phonon (on-device)"

    def __init__(self, model=None, voice: str | None = None, duration_delta: float = -0.25) -> None:
        if model is None:
            from driveos.voice.tts import load_phonon
            model = load_phonon(duration_delta)
        self._model = model
        self.voice = voice or None
        self._lock = threading.Lock()
        self._render("Ready.")                           # warm the graph so the first reply is not slow

    def _render(self, text: str) -> np.ndarray:
        parts: list[np.ndarray] = []
        with self._lock, self._model.stream(text, voice=self.voice) as audio:
            rate = int(audio.sample_rate)
            for chunk in audio:
                parts.append(np.asarray(chunk, dtype=np.float32).reshape(-1))
        x = np.concatenate(parts) if parts else np.zeros(0, np.float32)
        return resample(x, rate, TTS_SR) if rate != TTS_SR else x

    def _produce(self, text: str, loop: asyncio.AbstractEventLoop, q: asyncio.Queue, stop: threading.Event) -> None:
        """Worker thread: push resampled float chunks, then None (or the exception) as the sentinel."""
        end: object = None
        try:
            with self._lock, self._model.stream(text, voice=self.voice) as audio:
                rate = int(audio.sample_rate)
                for chunk in audio:
                    if stop.is_set():
                        break
                    x = np.asarray(chunk, dtype=np.float32).reshape(-1)
                    if rate != TTS_SR:
                        x = resample(x, rate, TTS_SR)
                    loop.call_soon_threadsafe(q.put_nowait, x)
        except Exception as e:                           # noqa: BLE001
            end = e
        loop.call_soon_threadsafe(q.put_nowait, end)

    async def synth_stream(self, text: str) -> AsyncIterator[Speech]:
        """Yield Speech chunks as Phonon generates them, clause by clause.

        A clause that ends at a dash, semicolon, ellipsis or colon is followed by a silent chunk with no words,
        so word timings stay continuous across the pause. ``TTS_CLAUSE_PAUSE_MS`` overrides the default.
        """
        pause_ms = _pause_ms()
        clauses = split_clauses(text, pause_ms) if pause_ms > 0 else [(text, 0)]
        for clause, pause in clauses or [(text, 0)]:
            async for chunk in self._clause_stream(clause):
                yield chunk
            if pause > 0:
                yield Speech(bytes(int(TTS_SR * pause / 1000) * 2), [])

    async def _clause_stream(self, text: str) -> AsyncIterator[Speech]:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()
        loop.run_in_executor(None, self._produce, text, loop, q, stop)
        words = text.split()
        weights = [_weight(w) for w in words]
        done = 0
        elapsed = 0.0
        try:
            while True:
                item = await q.get()
                if isinstance(item, Exception):
                    raise item
                if item is None:
                    break
                dur = len(item) / TTS_SR
                budget = (elapsed + dur) / _SEC_PER_WEIGHT
                nxt, acc = done, sum(weights[:done])
                while nxt < len(words) and acc + weights[nxt] / 2 <= budget:
                    acc += weights[nxt]
                    nxt += 1
                local = estimate_words(" ".join(words[done:nxt]), dur) if nxt > done else []
                done, elapsed = nxt, elapsed + dur
                yield Speech(to_pcm16(item), local)
            if done < len(words):                        # words the rate estimate has not placed yet
                yield Speech(b"", estimate_words(" ".join(words[done:]), 0.3))
        finally:
            stop.set()

    async def synth(self, text: str) -> Speech:
        pcm, words, t = [], [], 0.0
        async for c in self.synth_stream(text):
            pcm.append(c.pcm16)
            words += [(w, t + s, d) for w, s, d in c.words]
            t += c.duration
        return Speech(b"".join(pcm), words)


_SEC_PER_WEIGHT = 0.07
DEFAULT_PAUSE_MS = 220


def _pause_ms() -> int:
    try:
        return max(0, int(os.environ.get("TTS_CLAUSE_PAUSE_MS", DEFAULT_PAUSE_MS)))
    except ValueError:
        return DEFAULT_PAUSE_MS


def _weight(w: str) -> float:
    return len(w) + 2.5 + (3 if w.endswith((",", ".", "!", "?", "\u2014", ";", ":")) else 0)
