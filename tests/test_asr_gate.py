import asyncio

import numpy as np
import pytest

from cabin import frontend
from cabin.audio import MIC_SR, to_pcm16
from cabin.frontend.asr_gate import TranscriptGate, estimate_confidence
from cabin.providers.stt_parakeet import ParakeetSTT


def test_gate_drops_junk():
    g = TranscriptGate()
    for t in ("yet", "uh", "Um.", "the", "you", "hmm"):
        assert g.check(t, 0.9) == "drop", t


def test_gate_drops_fillers_only():
    g = TranscriptGate()
    for t in ("so", "and", "uh um", "the a"):
        assert g.check(t, 0.9) == "drop", t


def test_gate_accepts_single_content_words():
    g = TranscriptGate()
    for t in ("weather", "coffee", "home", "pharmacy", "Starbucks", "go home", "gas", "ATM", "map"):
        assert g.check(t, 0.9) == "ok", t


def test_gate_single_menu_item_via_vocabulary():
    g = TranscriptGate()
    assert g.check("latte", 0.9, ["Latte", "Blue Bottle Coffee"]) == "ok"
    assert g.check("lattee", 0.2, ["Latte"]) == "ok"
    assert g.check("it", 0.9) == "drop"


def test_gate_short_content_low_confidence_retries():
    assert TranscriptGate().check("pharmacy", 0.2) == "retry"


def test_gate_allows_short_commands():
    g = TranscriptGate()
    for t in ("yes", "No", "stop", "cancel", "okay", "approve", "back", "next", "two", "ten", "4 8 2 9 1 7"):
        assert g.check(t, 0.9) == "ok", t


def test_gate_retries_low_confidence():
    assert TranscriptGate().check("navigate to the nearest charger", 0.3) == "retry"


def test_gate_ok():
    assert TranscriptGate().check("what's the weather in Napa", 0.8) == "ok"


def test_confidence_from_speaking_rate():
    assert estimate_confidence("what is the weather in napa", 2.0) > 0.7
    assert estimate_confidence("a b c d e f g h i j k l m n", 1.0) < 0.5
    assert estimate_confidence("so what now", 8.0) < 0.5
    assert estimate_confidence("so what now why", 0.3) < 0.5


class FakeEngine:
    def __init__(self, text):
        self.text, self.vocab_seen = text, None

    def transcribe_sync(self, pcm):
        return self.text


def tone(sec):
    t = np.arange(int(MIC_SR * sec)) / MIC_SR
    return to_pcm16((0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32))


def run_session(text, vocab=None, sec=2.0):
    stt = ParakeetSTT(FakeEngine(text), vocabulary=vocab, gate=TranscriptGate())

    async def go():
        s = stt.session(None)
        await s.feed(tone(sec))
        return await s.finish()
    return asyncio.run(go())


@pytest.fixture
def retries(monkeypatch):
    got = []
    monkeypatch.setattr(frontend, "on_asr_retry", lambda: got.append(1))
    return got


def test_session_drops_junk_and_asks_again(retries):
    assert run_session("yet") == ""
    assert retries == []


def test_session_retries_on_implausible_rate(retries):
    assert run_session("one two three four five six seven eight nine ten eleven twelve", sec=0.8) == ""
    assert retries == [1]


def test_session_passes_good_transcript(retries):
    assert run_session("what's the weather in Napa") == "what's the weather in Napa"
    assert retries == []


def test_session_bypass_skips_gate(retries, monkeypatch):
    monkeypatch.setattr(frontend, "gate_bypass", lambda: True)
    assert run_session("uh") == "uh"


def test_vocabulary_repairs_names(monkeypatch):
    monkeypatch.setattr(frontend, "vocabulary_provider", lambda: ["Blue Bottle Coffee"] + [f"x{i}" for i in range(80)])
    out = run_session("take me to blu bottle coffee")
    assert "Blue Bottle Coffee" in out


def test_vocabulary_capped():
    from cabin.providers.stt_parakeet import MAX_VOCAB, clean_vocabulary
    v = clean_vocabulary([f"Place {i}" for i in range(200)] + ["Place 1", ""])
    assert len(v) == MAX_VOCAB == 50 and len(set(v)) == 50


def test_session_without_gate_returns_raw_text():
    async def go():
        s = ParakeetSTT(FakeEngine("uh")).session(None)
        await s.feed(tone(1.0))
        return await s.finish()
    assert asyncio.run(go()) == "uh"


def test_two_retries_in_a_row_speak_once():
    from cabin.frontend import plugin

    spoken = []

    class Eng:
        state = "idle"
        payment = None

        def emit(self, *a):
            pass

        async def _speak_loop(self, q):
            spoken.append(await q.get())

        def _set_state(self, *a):
            pass

    async def go():
        plugin.install(Eng())
        frontend.on_asr_retry()
        await asyncio.sleep(0.3)
        frontend.on_asr_retry()
        await asyncio.sleep(0.3)
        frontend.on_asr_ok()
        frontend.on_asr_retry()                 # inside the cooldown after the prompt
        await asyncio.sleep(0.2)
    saved = (frontend.on_asr_retry, frontend.on_asr_ok, frontend.vocabulary_provider, frontend.gate_bypass)
    try:
        asyncio.run(go())
    finally:
        (frontend.on_asr_retry, frontend.on_asr_ok, frontend.vocabulary_provider, frontend.gate_bypass) = saved
    assert spoken == ["Sorry, say that again?"]
