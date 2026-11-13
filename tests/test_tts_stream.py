"""Streaming TTS: chunks reach the player before synthesis ends; cancel closes the generator."""
from __future__ import annotations

import asyncio
import itertools
import time

import numpy as np

from cabin.audio import TTS_SR
from cabin.engine import Engine
from cabin.providers.tts_phonon import PhononTTS
from cabin.tts import Speech, estimate_words

TEXT = "Napa is sunny and warm today with a light breeze."


def _pcm(sec: float) -> bytes:
    return (np.ones(int(TTS_SR * sec)) * 1000).astype(np.int16).tobytes()


class FakePlayer:
    def __init__(self) -> None:
        self.t = 100.0
        self.enqueued: list[tuple[float, float]] = []   # (monotonic when queued, start)
        self.playing = False
        self.level_db = -40.0

    def enqueue(self, pcm16: bytes) -> float:
        start = self.t
        self.t += len(pcm16) / 2 / TTS_SR
        self.enqueued.append((time.monotonic(), start))
        return start


class FakeTTS:
    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.closed = False
        self.finished_at = 0.0
        self.gate = gate

    async def synth_stream(self, text: str):
        words = text.split()
        parts = [words[:3], words[3:6], words[6:]]
        try:
            for i, ws in enumerate(parts):
                yield Speech(_pcm(0.3), estimate_words(" ".join(ws), 0.3))
                if i == 0:
                    if self.gate is not None:
                        await self.gate.wait()
                    await asyncio.sleep(0.05)
            self.finished_at = time.monotonic()
        finally:
            self.closed = True


def _engine(tts) -> tuple[Engine, FakePlayer, list]:
    eng = Engine.__new__(Engine)
    eng.player, eng.tts, eng.state, eng._spoken = FakePlayer(), tts, "thinking", ""
    emitted: list = []
    eng.emit = lambda k, v: emitted.append((k, v))
    eng._set_state = lambda s, _="": setattr(eng, "state", s)
    return eng, eng.player, emitted


def test_chunks_enqueue_before_stream_ends():
    async def go():
        tts = FakeTTS()
        eng, player, emitted = _engine(tts)
        await eng._speak_stream(tts.synth_stream(TEXT), TEXT, [])
        return tts, eng, player, emitted

    tts, eng, player, emitted = asyncio.run(go())
    assert len(player.enqueued) == 3
    assert player.enqueued[0][0] < tts.finished_at
    assert eng.state == "speaking" and eng._spoken.count(TEXT) == 1
    words = [w for k, v in emitted if k == "words" for w in v]
    assert [w[0] for w in words] == TEXT.split()
    starts = [w[1] for w in words]
    assert starts == sorted(starts)
    assert all(b[1] >= a[1] + a[2] - 1e-6 for a, b in itertools.pairwise(words))


def test_cancel_mid_stream_closes_generator():
    async def go():
        gate = asyncio.Event()
        tts = FakeTTS(gate)
        eng, player, _ = _engine(tts)
        task = asyncio.ensure_future(eng._speak_stream(tts.synth_stream(TEXT), TEXT, []))
        await asyncio.sleep(0.1)
        assert len(player.enqueued) == 1 and not tts.closed
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return tts

    assert asyncio.run(go()).closed


def test_phonon_words_cover_text_across_chunks():
    class Audio:
        sample_rate = TTS_SR

        def __init__(self, n):
            self.n = n

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            for _ in range(self.n):
                yield np.zeros(int(TTS_SR * 0.8), np.float32)

    class Model:
        def stream(self, text, voice=None):
            return Audio(3)

    tts = PhononTTS(model=Model())

    async def go():
        return [c async for c in tts.synth_stream(TEXT)]

    chunks = asyncio.run(go())
    assert len(chunks) >= 3
    flat = [w for c in chunks for w in c.words]
    assert [w[0] for w in flat] == TEXT.split()
    for c in chunks:
        starts = [w[1] for w in c.words]
        assert starts == sorted(starts)

    sp = asyncio.run(tts.synth(TEXT))
    assert [w[0] for w in sp.words] == TEXT.split()
    starts = [w[1] for w in sp.words]
    assert starts == sorted(starts)
