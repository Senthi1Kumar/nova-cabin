"""Shared-mic device resolution and the playback reference tap."""
import time

import numpy as np

from cabin import audio

DEVICES = [
    {"name": "hw:2,0 reSpeaker XVF3800", "max_input_channels": 2, "max_output_channels": 2},
    {"name": "pipewire", "max_input_channels": 64, "max_output_channels": 64},
    {"name": "default", "max_input_channels": 64, "max_output_channels": 64},
    {"name": "HDA Intel PCH: ALC (hw:0,0)", "max_input_channels": 2, "max_output_channels": 2},
]


class FakeSD:
    def __init__(self, devices):
        self._d = devices

    def query_devices(self, device=None, kind=None):
        if device is None:
            return list(self._d)
        return self._d[device]


def _patch(monkeypatch, devices=DEVICES, shared=None):
    monkeypatch.setattr(audio, "sd", FakeSD(devices))
    if shared is None:
        monkeypatch.delenv("AUDIO_SHARED", raising=False)
    else:
        monkeypatch.setenv("AUDIO_SHARED", shared)


def test_resolve_device_prefers_pipewire_over_hw(monkeypatch):
    _patch(monkeypatch)
    assert audio.resolve_device("XVF3800") == 1          # pipewire
    _patch(monkeypatch, shared="false")
    assert audio.resolve_device("XVF3800") == 0          # raw hw


def test_resolve_device_prefers_named_pipewire_source(monkeypatch):
    devs = DEVICES + [{"name": "reSpeaker XVF3800 4-Mic Array", "max_input_channels": 1,
                       "max_output_channels": 0}]
    _patch(monkeypatch, devs)
    assert audio.resolve_device("xvf3800") == 4


def test_resolve_device_passthrough(monkeypatch):
    _patch(monkeypatch)
    assert audio.resolve_device("") is None
    assert audio.resolve_device("3") == 3
    assert audio.resolve_device("nothing-here") == 1     # shared default device
    _patch(monkeypatch, shared="false")
    assert audio.resolve_device("nothing-here") == "nothing-here"


def test_reference_tap_matches_played_samples(monkeypatch):
    monkeypatch.setattr(audio, "sd", None)
    p = audio.Player(None)
    p.sr = audio.TTS_SR
    p.latency = 0.05
    assert p.reference_delay_s == 0.05
    t = np.arange(int(0.3 * audio.MIC_SR)) / audio.MIC_SR
    tone = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    t_call = time.monotonic()
    start = p.enqueue(audio.to_pcm16(tone), sr=audio.MIC_SR)
    assert abs((start - t_call) - p.reference_delay_s) < 0.02
    time.sleep(0.7)
    now = time.monotonic()
    x = p.reference.read(audio.MIC_SR)
    assert x.dtype == np.float32 and len(x) == audio.MIC_SR
    peak = np.flatnonzero(np.abs(x) > 0.05)
    assert len(peak)
    onset_t = now - (len(x) - peak[0]) / audio.MIC_SR
    assert abs((onset_t - t_call) - p.reference_delay_s) < 0.02
    seg = x[peak[0]:peak[0] + len(tone)]
    n = min(len(seg), len(tone))
    corr = np.max(np.correlate(seg[:n], tone[:n], "full"))
    corr /= np.linalg.norm(seg[:n]) * np.linalg.norm(tone[:n]) + 1e-9
    assert corr > 0.95


def test_reference_tap_idle_is_zeros():
    tap = audio.ReferenceTap()
    assert not tap.read(1600).any()
