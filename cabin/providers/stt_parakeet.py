"""On-device speech-to-text with Parakeet (via DriveOS' ctypes wrapper).

The session buffers 16 kHz PCM and transcribes the whole utterance in ``finish()`` on a worker
thread. With ``partials`` enabled, every ``PARTIAL_EVERY_S`` of new audio the chunk since the last
boundary is transcribed and the running text is pushed to ``on_partial``.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path

from .. import frontend
from ..audio import MIC_SR
from ..frontend.asr_gate import TranscriptGate, estimate_confidence

log = logging.getLogger("stt.parakeet")
Partial = Callable[[str], Awaitable[None] | None]

PARTIAL_EVERY_S = 1.2
MIN_FINAL_S = 0.25
BYTES_PER_S = MIC_SR * 2
MAX_VOCAB = 50


def clean_vocabulary(terms) -> list[str]:
    """Unique, non-empty terms in order, capped at MAX_VOCAB."""
    out, seen = [], set()
    for t in terms or ():
        t = str(t).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
            if len(out) == MAX_VOCAB:
                break
    return out


# One recogniser engine serves the wake check, partials and the final transcript: one call at a time.
_ENGINE_LOCK = threading.Lock()


class _ParakeetSession:
    def __init__(self, owner: ParakeetSTT, on_partial: Partial | None) -> None:
        self.owner = owner
        self.on_partial = on_partial if owner.partials else None
        self._pcm = bytearray()
        self._chunk_start = 0
        self._chunk_texts: list[str] = []
        self._task: asyncio.Task | None = None
        self._closed = False

    async def feed(self, pcm: bytes) -> None:
        if self._closed:
            return
        self._pcm.extend(pcm)
        if (self.on_partial and (self._task is None or self._task.done())
                and len(self._pcm) - self._chunk_start >= PARTIAL_EVERY_S * BYTES_PER_S):
            end = len(self._pcm)
            chunk = bytes(self._pcm[self._chunk_start:end])
            self._chunk_start = end
            self._task = asyncio.create_task(self._partial(chunk))

    async def _partial(self, chunk: bytes) -> None:
        try:
            text = await asyncio.to_thread(self.owner.transcribe_sync, chunk)
        except Exception as e:                                       # noqa: BLE001
            log.debug("partial failed: %s", e)
            return
        if self._closed or not text:
            return
        self._chunk_texts.append(text)
        res = self.on_partial(" ".join(self._chunk_texts))
        if asyncio.iscoroutine(res):
            await res

    async def finish(self) -> str:
        self._closed = True
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        if len(self._pcm) < MIN_FINAL_S * BYTES_PER_S:
            return ""
        pcm = bytes(self._pcm)
        vocab = self._vocabulary()
        text = (await asyncio.to_thread(self.owner.transcribe_sync, pcm, vocab)).strip()
        if not text or self.owner.gate is None or frontend.gate_bypass():
            return text
        verdict = self.owner.gate.check(text, estimate_confidence(text, len(pcm) / BYTES_PER_S), vocab)
        if verdict == "ok":
            frontend.on_asr_ok()
            return text
        log.info("transcript gate: %s", verdict)
        if verdict == "retry":
            try:
                frontend.on_asr_retry()
            except Exception:
                log.exception("retry prompt failed")
        return ""

    def _vocabulary(self) -> list[str]:
        try:
            return clean_vocabulary(frontend.vocabulary_provider())
        except Exception:                                            # noqa: BLE001
            return []

    async def cancel(self) -> None:
        self._closed = True
        self._pcm.clear()
        if self._task is not None:
            self._task.cancel()


class ParakeetSTT:
    label = "Parakeet (on-device)"

    def __init__(self, engine, partials: bool = False, vocabulary=None, gate: TranscriptGate | None = None) -> None:
        self.engine = engine
        self.partials = partials
        self.gate = gate
        self._static_vocabulary = vocabulary

    def transcribe_sync(self, pcm16: bytes, vocabulary=None) -> str:
        with _ENGINE_LOCK:
            text = self.engine.transcribe_sync(pcm16)
        names = clean_vocabulary(vocabulary if vocabulary is not None else self._static_vocabulary)
        if text and names:
            try:
                from driveos.voice.entity_fix import (
                    entities_from_vocabulary,
                    fix_entities,
                )
                text = fix_entities(text, entities_from_vocabulary(", ".join(names)))
            except Exception as e:                                   # noqa: BLE001
                log.debug("name repair unavailable: %s", e)
        return text

    def session(self, on_partial: Partial | None = None):
        return _ParakeetSession(self, on_partial)

    def close(self) -> None:
        close = getattr(self.engine, "close", None)
        if close:
            close()


def load(lib: str = "", model: str = "", partials: bool = False, gate: bool = False) -> ParakeetSTT | None:
    """Load Parakeet from explicit paths or DriveOS' model directory; None when lib/model/driveos are absent."""
    try:
        from driveos.config import VoiceConfig
        from driveos.voice.parakeet_stt import ParakeetSTT as Engine
        from driveos.voice.parakeet_stt import resolve_paths
    except Exception as e:                                           # noqa: BLE001
        log.info("parakeet unavailable: %s", e)
        return None
    cfg = VoiceConfig(parakeet_lib=Path(lib) if lib else None, parakeet_model=Path(model) if model else None)
    lib_path, model_path = resolve_paths(cfg)
    if lib_path is None or model_path is None:
        log.info("parakeet library or model not found")
        return None
    try:
        return ParakeetSTT(Engine(lib_path, model_path), partials, gate=TranscriptGate() if gate else None)
    except Exception as e:                                           # noqa: BLE001
        log.warning("parakeet failed to load: %s", e)
        return None
