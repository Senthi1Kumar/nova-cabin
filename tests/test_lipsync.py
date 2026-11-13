"""Lip sync: envelope-driven jaw, playback-clock timing, stop latency, Player position reports."""
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cabin import audio
from cabin.lipsync import (
    LipSyncDriver,
    VisemeTrack,
    blend_jaw,
    envelope_to_jaw,
    word_visemes,
)


class Ctrl:
    def __init__(self):
        self.last = (0.0, 0.0, False)
        self.history = []

    def set_viseme(self, jaw, rnd, live):
        self.last = (jaw, rnd, live)
        self.history.append((time.monotonic(), jaw, rnd, live))


def burst(amp: float, ms: int, sr: int = 24000, hz: float = 1000.0) -> np.ndarray:
    t = np.arange(sr * ms // 1000) / sr
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))


def make_driver():
    c = Ctrl()
    d = LipSyncDriver(c, start_timer=False)
    return c, d


def run(d, c, t0, seconds, env_by_t, step=0.016):
    """Feed envelope samples every 20 ms and tick at 60 Hz on a virtual clock."""
    t = t0
    next_rep = t0
    while t < t0 + seconds:
        while next_rep <= t:
            d.on_position(next_rep, env_by_t(next_rep))
            next_rep += 0.02
        d.tick(t)
        t += step
    return t


def test_loud_burst_opens_jaw_and_silence_closes_it():
    c, d = make_driver()
    loud = rms(burst(0.3, 100))
    d.add_word("father", 0.0, 1.0)
    run(d, c, 0.0, 0.3, lambda t: loud)
    assert c.last[0] > 0.4
    run(d, c, 0.3, 0.4, lambda t: 0.0)
    assert c.last[0] < 0.1
    d.close()


def test_envelope_mapping_bounds():
    assert envelope_to_jaw(0.0) == 0.0
    assert envelope_to_jaw(0.3) > 0.9
    assert envelope_to_jaw(0.001) == 0.0


def test_round_comes_from_text_visemes():
    assert "UW" in word_visemes("moon")
    c, d = make_driver()
    d.add_word("oo", 0.0, 0.4)
    run(d, c, 0.0, 0.2, lambda t: 0.1)
    assert c.last[1] > 0.5
    d.close()


def test_lip_closure_survives_blend():
    assert blend_jaw(0.5, -0.35) > blend_jaw(0.5, 0.95) - 1.0
    assert blend_jaw(0.0, 0.95) == 0.0


def test_envelope_applies_at_its_playback_timestamp():
    c, d = make_driver()
    d.add_word("father", 0.0, 2.0)
    d.on_position(1.0, 0.0)                     # a pause that is audible only at t=1.0
    for k in range(10):
        d.tick(0.5 + k * 0.016)
    assert c.last[0] > 0.8                      # before its time the viseme alone drives the jaw
    d.tick(1.0)
    d.tick(1.016)
    assert c.last[0] < 0.1                      # the playing audio is silent: mouth rests
    d.close()


def test_mouth_closes_within_60ms_of_stop():
    c, d = make_driver()
    loud = rms(burst(0.3, 100))
    d.add_word("father", 0.0, 2.0)
    run(d, c, 0.0, 0.3, lambda t: loud)
    assert c.last[0] > 0.4
    t_stop = time.monotonic()
    d.stop()
    assert c.last == (0.0, 0.0, False)
    assert c.history[-1][0] - t_stop < 0.06
    d.on_position(0.31, loud)                   # stale audio after stop is ignored until new words
    d.tick(0.32)
    assert c.last == (0.0, 0.0, False)
    d.close()


def test_silent_speech_falls_back_to_visemes():
    c, d = make_driver()
    d.add_word("father", 0.0, 0.6)
    for k in range(10):
        d.tick(0.1 + k * 0.016)
    assert c.last[2] and c.last[0] > 0.3
    d.close()


def test_viseme_track_rests_after_gap():
    v = VisemeTrack()
    v.add_word("oo", 0.0, 0.3)
    assert v.sample(10.0) == (0.0, 0.0)


def test_player_reports_envelope_on_playback_clock():
    reports = []
    p = audio.Player(device=None)
    p.on_position = lambda t, r: reports.append((t, r))
    start = p.enqueue((burst(0.3, 200) * 32767).astype(np.int16).tobytes())
    time.sleep(0.45)
    assert reports, "no position reports"
    loud = [r for t, r in reports if r > 0.1]
    assert loud
    assert abs(reports[0][0] - start) < 0.05
    assert reports[-1][1] == 0.0                # drain reports silence
    p.close()
