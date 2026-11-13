import asyncio
import wave
from pathlib import Path

import numpy as np
import pytest

from cabin.audio import MIC_SR, VAD, VADConfig, to_pcm16
from cabin.config import Settings
from cabin.providers.stt_parakeet import ParakeetSTT
from cabin.providers.turn import models_present
from cabin.stt import make_stt


class FakeEngine:
    def __init__(self):
        self.calls = []

    def transcribe_sync(self, pcm):
        self.calls.append(len(pcm))
        return f"heard {len(pcm) // 32000}s"


def tone(sec, amp=0.3):
    t = np.arange(int(MIC_SR * sec)) / MIC_SR
    return to_pcm16((amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32))


def test_session_transcribes_on_finish():
    async def go():
        s = ParakeetSTT(FakeEngine()).session(None)
        for i in range(0, len(tone(2)), 640):
            await s.feed(tone(2)[i:i + 640])
        return await s.finish()
    assert asyncio.run(go()) == "heard 2s"


def test_short_audio_is_empty():
    async def go():
        s = ParakeetSTT(FakeEngine()).session(None)
        await s.feed(tone(0.1))
        return await s.finish()
    assert asyncio.run(go()) == ""


def test_partials_emit_chunks():
    got = []

    async def go():
        s = ParakeetSTT(FakeEngine(), partials=True).session(lambda t: got.append(t))
        pcm = tone(3)
        for i in range(0, len(pcm), 640):
            await s.feed(pcm[i:i + 640])
            await asyncio.sleep(0)
            await asyncio.sleep(0.005)
        return await s.finish()
    assert asyncio.run(go()) == "heard 3s"
    assert got and got[0].startswith("heard")


def test_factory_picks_parakeet_explicitly(monkeypatch):
    import cabin.providers.stt_parakeet as mod
    monkeypatch.setattr(mod, "load", lambda *a, **k: ParakeetSTT(FakeEngine()))
    p = make_stt(Settings({"STT_PROVIDER": "parakeet"}), None)
    assert p.label == "Parakeet (on-device)"


def test_factory_auto_falls_back_without_parakeet(monkeypatch):
    import cabin.providers.stt_parakeet as mod
    monkeypatch.setattr(mod, "load", lambda *a, **k: None)
    assert make_stt(Settings({"STT_PROVIDER": "auto"}), None) is None
    p = make_stt(Settings({"STT_PROVIDER": "auto", "GROQ_API_KEY": "x"}), None)
    assert p.label == "Groq"


def test_energy_vad_is_default_when_forced():
    v = VAD(VADConfig(), engine="energy")
    assert v.engine == "energy"
    ev, level = v.update(np.zeros(320, np.float32), None)
    assert ev is None and 0 <= level <= 1


SPEECH = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "asr" / "set_ac_20.wav"


@pytest.mark.skipif(not (models_present() and SPEECH.exists()), reason="silero model or speech fixture missing")
def test_silero_ends_short_utterance_before_fixed_silence():
    pytest.importorskip("onnxruntime")
    with wave.open(str(SPEECH)) as w:
        full = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    speech = full[8000:24000]                                   # one second of speech
    sig = np.concatenate([np.zeros(6400, np.float32), speech, np.zeros(int(MIC_SR * 1.2), np.float32)])
    v = VAD(VADConfig(end_silence_ms=700), engine="silero")
    assert v.engine == "silero"
    events = {}
    for i in range(len(sig) // 320):
        ev, _ = v.update(sig[i * 320:(i + 1) * 320], None)
        if ev:
            events.setdefault(ev, i * 0.02)
    speech_end = (6400 + len(speech)) / MIC_SR
    assert "start" in events and "end" in events
    assert events["end"] - speech_end < 0.6
