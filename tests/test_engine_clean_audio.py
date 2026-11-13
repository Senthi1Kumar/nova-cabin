"""Engine takes STT and pre-roll audio from the echo-cancelled stream when software AEC is on."""
import asyncio
import collections
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from cabin.audio import to_pcm16
from cabin.config import Settings
from cabin.engine import Engine

ROOT = Path(__file__).resolve().parent.parent
N = 320


def raw(i):
    return np.full(N, 0.1 + i * 0.001, np.float32)


def clean(i):
    return np.full(N, -0.2 - i * 0.001, np.float32)


class Session:
    def __init__(self):
        self.fed = []

    async def feed(self, pcm):
        self.fed.append(pcm)


class FakeVad:
    def __init__(self, aec):
        self.cfg = SimpleNamespace(preroll_ms=20 * 40, max_utterance_s=1e9)
        self.seen = []
        self._frontend = SimpleNamespace(stats={"aec": aec}, clean_frames=collections.deque(maxlen=64))

    def update(self, frame, playback_db):
        self.seen.append(frame)
        return None, 0.0


def make(aec):
    cfg = Settings.load(ROOT / "tests" / "empty.env")
    eng = Engine(cfg, lambda k, v: None, Path(tempfile.mkdtemp()), sim=True)
    eng.vad = FakeVad(aec)
    eng.player = SimpleNamespace(playing=False, level_db=-90.0)
    eng.state = "listening"
    eng._listen_session = Session()
    eng._heard_speech = True
    eng._listen_started = eng._listen_armed_at = 10 ** 12
    return eng


def run(eng, n, lag=2, stall_after=None):
    async def go():
        for i in range(n):
            f = raw(i)
            eng._on_frame(f)
            assert eng.vad.seen[-1] is f
            j = i - lag
            if j >= 0 and (stall_after is None or j < stall_after):
                eng.vad._frontend.clean_frames.append(clean(j))
        await asyncio.sleep(0)
    asyncio.run(go())


def test_aec_on_feeds_clean_in_order():
    eng = make("on")
    run(eng, 30)
    k = len(eng._listen_session.fed)
    assert k == 27
    assert eng._listen_session.fed == [to_pcm16(clean(j)) for j in range(k)]
    assert list(eng._preroll) == eng._listen_session.fed


def test_preroll_is_clean():
    eng = make("on")
    eng.state = "idle"
    run(eng, 12)
    assert list(eng._preroll) == [to_pcm16(clean(j)) for j in range(9)]


def test_aec_off_feeds_raw():
    for mode in ("off", "hardware"):
        eng = make(mode)
        run(eng, 10)
        assert eng._listen_session.fed == [to_pcm16(raw(i)) for i in range(10)]
        assert list(eng._preroll) == eng._listen_session.fed


def test_stalled_clean_stream_falls_back_to_raw(caplog):
    eng = make("on")
    with caplog.at_level("WARNING"):
        run(eng, 30, stall_after=5)
    fed = eng._listen_session.fed
    assert fed[:5] == [to_pcm16(clean(j)) for j in range(5)]
    assert fed[-1] == to_pcm16(raw(29))
    assert to_pcm16(raw(20)) in fed and to_pcm16(clean(5)) not in fed
    assert sum("stalled" in r.message for r in caplog.records) == 1
