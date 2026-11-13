"""Wake phrase matching and the front-end stage that drops utterances without it."""
import time

import numpy as np
import pytest

from cabin import frontend
from cabin.audio import VadStage
from cabin.config import Settings
from cabin.frontend import FrontEnd
from cabin.frontend.gate import WakeStage
from cabin.providers.wake import WakeMatcher

MATCH = WakeMatcher(["hey nova"])


@pytest.mark.parametrize("text,rest", [
    ("hey nova find coffee", "find coffee"),
    ("Hey Nova, find coffee.", "find coffee."),
    ("hi nova", ""),
    ("ok nova what's the weather", "what's the weather"),
    ("okay nova", ""),
    ("nova, play jazz", "play jazz"),
    ("hey noah find coffee", "find coffee"),
    ("hey nowa find coffee", "find coffee"),
    ("hay nova find coffee", "find coffee"),
    ("Hey, Nova! Navigate home", "Navigate home"),
])
def test_wake_matcher_fuzzy(text, rest):
    assert MATCH.match(text) == (True, rest)


@pytest.mark.parametrize("text", [
    "hey nobody", "Noah, pass the salt", "Nora said she would call", "noah find coffee", "find a nova", "a nova", "the weather today", "", "hey",
    "please ask nova to find coffee", "navigate to nova", "hey there nova",
])
def test_wake_matcher_rejects(text):
    assert MATCH.match(text)[0] is False


def test_wake_matcher_custom_phrase():
    m = WakeMatcher(["hey aria"])
    assert m.match("hey aria lights on") == (True, "lights on")
    assert m.match("hey nova lights on")[0] is False


def _frame(level=0.2):
    return np.full(320, level, np.float32)


class _Vad:
    """Emits start on frame 3, end on frame `end_at`."""

    def __init__(self, end_at):
        self.n = 0
        self.end_at = end_at
        self.in_speech = False

    def detect(self, frame, playback_db):
        self.n += 1
        if self.n == 3:
            self.in_speech = True
            return "start", 0.5
        if self.n == self.end_at:
            self.in_speech = False
            return "end", 0.1
        return None, 0.4


def _front(text, end_at=500, wake="hey nova", required=None):
    got = []
    heard = []
    vad = _Vad(end_at)
    cfg = Settings({"WAKE_WORD": wake, "WAKE_WINDOW_S": "0.4", "DRIVER_GATE": "off"})
    fe = FrontEnd(cfg, got.append, stages=[VadStage(vad.detect)])
    wake = next((s for s in fe.stages if isinstance(s, WakeStage)), None)
    if wake is not None:
        wake._transcribe = lambda pcm: (heard.append(len(pcm)), text)[1]
        wake._required = required or (lambda: True)
    return fe, got, heard


def run(fe, n):
    """Process ``n`` frames, then more until no wake verdict is pending."""
    wake = next((s for s in fe.stages if isinstance(s, WakeStage)), None)
    i = 0
    while i < n or (wake is not None and wake._job is not None and i < n + 2000):
        fe.process(_frame(), None, float(i) * 0.02, i + 1)
        time.sleep(0.001)
        i += 1


def kinds(got):
    return [e.kind for e in got if e.kind != "level"]


def test_wake_drop_never_reaches_engine():
    fe, got, heard = _front("the weather today", end_at=60)
    run(fe, 80)
    assert heard, "the window was never transcribed"
    assert "start" not in kinds(got) and "end" not in kinds(got)


def test_wake_match_emits_start_late_and_wake_text():
    fe, got, heard = _front("hey nova find coffee", end_at=60)
    run(fe, 40)
    assert kinds(got) == ["wake", "start"]
    wake = next(e for e in got if e.kind == "wake")
    assert wake.text == "find coffee"
    assert len(heard) == 1
    run(fe, 70)
    assert kinds(got)[-1] == "end"


def test_wake_short_utterance_decides_on_end():
    fe, got, _ = _front("hey nova", end_at=8)
    run(fe, 12)
    assert kinds(got) == ["wake", "start", "end"]


def test_wake_not_required_passes_start_immediately():
    fe, got, heard = _front("the weather today", required=lambda: False)
    run(fe, 4)
    assert "start" in kinds(got) and not heard


def test_wake_disabled_without_phrase():
    fe, got, heard = _front("anything", wake="")
    run(fe, 4)
    assert "start" in kinds(got) and not heard


def test_wake_transcriber_missing_fails_open():
    fe, got, _ = _front("x")
    next(s for s in fe.stages if isinstance(s, WakeStage))._transcribe = lambda pcm: None
    run(fe, 40)
    assert "start" in kinds(got)


def test_late_start_carries_onset_time():
    fe, got, _ = _front("hey nova find coffee", end_at=500)
    run(fe, 40)
    start = next(e for e in got if e.kind == "start")
    assert abs(start.onset_t - 2 * 0.02) < 1e-6          # the VAD fired on frame 3


def test_slow_transcriber_never_blocks_the_worker():
    cfg = Settings({"WAKE_WORD": "hey nova", "WAKE_WINDOW_S": "0.2", "DRIVER_GATE": "off"})
    vad = _Vad(500)
    got = []
    fe = FrontEnd(cfg, got.append, stages=[VadStage(vad.detect)])
    wake = next(s for s in fe.stages if isinstance(s, WakeStage))
    wake._transcribe = lambda pcm: (time.sleep(1.0), "hey nova go")[1]
    fe.start()
    t0 = time.monotonic()
    for i in range(100):                                  # two seconds of 20 ms frames
        fe.push(_frame(), None, float(i) * 0.02)
        time.sleep(0.02)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and fe.stats["frames"] < 100:
        time.sleep(0.01)
    fe.stop()
    wake.close()
    assert fe.stats["dropped"] == 0 and fe.stats["frames"] == 100
    if fe.stats.get("aec") == "on":
        assert len(fe.clean_frames) >= 60
    assert time.monotonic() - t0 < 4
    assert "start" in kinds(got)


def test_hooks_have_defaults():
    assert frontend.wake_required() is True
    assert frontend.wake_transcriber() is None
