"""Engine wake check, late-start pre-roll and clean-audio fallback reset."""
import asyncio
import collections
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from cabin import frontend
from cabin.audio import FRAME_MS, Frame, to_pcm16
from cabin.config import Settings
from cabin.engine import Engine

ROOT = Path(__file__).resolve().parent.parent
N = 320


def engine(wake="hey nova"):
    cfg = Settings.load(ROOT / "tests" / "empty.env")
    eng = Engine(cfg, lambda k, v: None, Path(tempfile.mkdtemp()), sim=True)
    eng.wake = wake
    eng.heard = []

    async def respond(text):
        eng.heard.append(text)
    eng._respond = respond
    return eng


class Done:
    def __init__(self, text):
        self.text = text

    async def finish(self):
        return self.text


def hear(eng, text):
    asyncio.run(eng._from_voice(Done(text)))
    return eng.heard


def test_wake_phrase_with_punctuation_is_stripped(monkeypatch):
    monkeypatch.setattr(frontend, "wake_required", lambda: True)
    assert hear(engine(), "Hey, Nova! find coffee") == ["find coffee"]


def test_recogniser_spelling_passes(monkeypatch):
    monkeypatch.setattr(frontend, "wake_required", lambda: True)
    assert hear(engine(), "hey noah find coffee") == ["find coffee"]


def test_no_wake_phrase_is_dropped(monkeypatch):
    monkeypatch.setattr(frontend, "wake_required", lambda: True)
    assert hear(engine(), "the weather today") == []


def test_exempt_utterance_passes_untouched(monkeypatch):
    monkeypatch.setattr(frontend, "wake_required", lambda: False)
    eng = engine()
    assert hear(eng, "4 2 7 1") == ["4 2 7 1"]
    assert hear(eng, "yes") == ["4 2 7 1", "yes"]


def test_no_wake_word_configured_never_checks(monkeypatch):
    def boom():
        raise AssertionError("consulted")
    monkeypatch.setattr(frontend, "wake_required", boom)
    assert hear(engine(wake=""), "find coffee") == ["find coffee"]


class Session:
    def __init__(self):
        self.fed = []

    async def feed(self, pcm):
        self.fed.append(pcm)


class Vad:
    def __init__(self, onset=0.0):
        self.cfg = SimpleNamespace(preroll_ms=20 * 40, max_utterance_s=1e9)
        self.last_onset_t = onset

    def update(self, frame, playback_db):
        return None, 0.0


class Stt:
    def __init__(self):
        self.made = []

    def session(self, partial):
        s = Session()
        self.made.append(s)
        return s


def frame(i, t0=100.0):
    f = np.full(N, 0.001 * (i + 1), np.float32).view(Frame)
    f.t = t0 + (i + 1) * FRAME_MS / 1000
    return f


def prerolled(onset_after):
    eng = engine()
    eng.vad = Vad()
    eng.player = SimpleNamespace(playing=False, level_db=-90.0)
    eng.stt = Stt()
    eng.state = "idle"
    eng.handsfree, eng.voice_ok = True, True
    n = 120                                    # 2.4 s of audio
    frames = [frame(i) for i in range(n)]
    for f in frames[:-1]:
        eng._on_frame(f)
    return eng, frames, onset_after


def test_late_start_feeds_from_onset_minus_300ms():
    eng, frames, _ = prerolled(0)
    onset = frames[-1].t - 1.5
    eng.vad.last_onset_t = onset

    async def go():
        eng.vad.update = lambda f, p: ("start", 0.5)
        eng._on_frame(frames[-1])
        await asyncio.sleep(0)
    asyncio.run(go())
    fed = eng.stt.made[-1].fed
    ts = [f.t for f in frames if f.t >= onset - 0.3 - 1e-6]
    assert fed == [to_pcm16(f) for f in frames if f.t >= onset - 0.3 - 1e-6]
    assert len(fed) == len(ts)
    assert to_pcm16(frames[0]) not in fed


def test_start_without_onset_uses_old_preroll():
    eng, frames, _ = prerolled(0)

    async def go():
        eng.vad.update = lambda f, p: ("start", 0.5)
        eng._on_frame(frames[-1])
        await asyncio.sleep(0)
    asyncio.run(go())
    assert len(eng.stt.made[-1].fed) == eng.vad.cfg.preroll_ms // FRAME_MS


def test_clean_failure_resets_each_turn():
    eng = engine()
    eng.vad = Vad()
    eng.stt = Stt()
    eng._clean_failed = True

    async def go():
        eng._begin_listen(heard=False)
    asyncio.run(go())
    assert eng._clean_failed is False


def _finish_listen(eng, text, *, heard):
    async def go():
        eng.stt = Stt()
        eng._begin_listen(heard=heard)
        eng._listen_session = Done(text)
        eng._end_listen()
        await eng._turn
    asyncio.run(go())
    return eng.heard


def test_tap_keeps_a_bare_answer(monkeypatch):
    monkeypatch.setattr(frontend, "wake_required", lambda: True)
    eng = engine()
    assert _finish_listen(eng, "yes", heard=False) == ["yes"]


def test_handsfree_bare_answer_is_dropped_when_wake_is_required(monkeypatch):
    monkeypatch.setattr(frontend, "wake_required", lambda: True)
    eng = engine()
    assert _finish_listen(eng, "yes", heard=True) == []


def _hook(monkeypatch, eng):
    from cabin.frontend.plugin import needs_wake
    monkeypatch.setattr(frontend, "wake_required", lambda: needs_wake(eng))


def test_yes_just_after_playback_needs_no_wake_phrase(monkeypatch):
    eng = engine()
    eng._playback_ended_at = time.monotonic()
    _hook(monkeypatch, eng)
    assert _finish_listen(eng, "yes", heard=True) == ["yes"]


def test_yes_long_after_playback_still_needs_the_wake_phrase(monkeypatch):
    eng = engine()
    eng._playback_ended_at = time.monotonic() - 30
    _hook(monkeypatch, eng)
    assert _finish_listen(eng, "yes", heard=True) == []
    assert _finish_listen(eng, "hey nova yes", heard=True) == ["yes"]


def test_open_checkout_accepts_approve_without_the_wake_phrase(monkeypatch):
    eng = engine()
    eng.payment = SimpleNamespace(co=object(), snapshot=lambda: {"otp_pending": False})
    _hook(monkeypatch, eng)
    assert _finish_listen(eng, "approve", heard=True) == ["approve"]


def test_pending_code_accepts_digits_without_the_wake_phrase(monkeypatch):
    eng = engine()
    eng.payment = SimpleNamespace(co=None, snapshot=lambda: {"otp_pending": True})
    _hook(monkeypatch, eng)
    assert _finish_listen(eng, "4271", heard=True) == ["4271"]


def test_barge_in_feeds_from_the_onset():
    eng, frames, _ = prerolled(0)
    eng.state = "thinking"
    eng.barge_in = True
    eng.player.flush = lambda: None
    onset = frames[-1].t - 1.5
    eng.vad.last_onset_t = onset

    async def go():
        eng.vad.update = lambda f, p: ("start", 0.5)
        eng._on_frame(frames[-1])
        await asyncio.sleep(0)
    asyncio.run(go())
    fed = eng.stt.made[-1].fed
    assert fed == [to_pcm16(f) for f in frames if f.t >= onset - 0.3 - 1e-6]
    assert to_pcm16(frames[0]) not in fed


def test_a_tap_does_not_require_the_wake_phrase():
    from cabin.frontend.plugin import needs_wake
    eng = engine()
    eng._from_tap = True
    assert needs_wake(eng) is False
    eng._from_tap = False
    eng._playback_ended_at = 0
    assert needs_wake(eng) is True


def test_frame_level_follows_the_mic():
    from cabin.engine import frame_level
    assert frame_level(np.zeros(320, np.float32)) == 0.0
    assert abs(frame_level(np.full(320, 0.1, np.float32)) - 0.4) < 1e-5
    assert frame_level(np.full(320, 0.5, np.float32)) == 1.0


def test_clean_failure_resets_when_front_end_restarts():
    eng = engine()
    fe1 = SimpleNamespace(stats={"aec": "on"}, clean_frames=collections.deque(maxlen=64))
    eng.vad = SimpleNamespace(cfg=SimpleNamespace(preroll_ms=800), _frontend=fe1)
    f = frame(0)
    eng._mic_chunks(f)
    eng._clean_failed = True
    assert eng._mic_chunks(f) == [to_pcm16(f)]
    assert eng._clean_failed
    eng.vad._frontend = SimpleNamespace(stats={"aec": "on"}, clean_frames=collections.deque(maxlen=64))
    eng._mic_chunks(f)
    assert eng._clean_failed is False
