"""Payment wiring: mic frame listeners, the ring DriveOS reads, bridge reset, real voice approving a checkout."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from driveos.wallet.auth import AuthResult, AuthTier
from test_payment import arun, cart, home, make, seed_known, wait_task  # noqa: F401

from cabin import audio
from cabin.services.driveauth_bridge import MicRing, VerifierBridge


def frame(v: float = 0.1) -> np.ndarray:
    return np.full(audio.FRAME, v, dtype=np.float32)


@pytest.fixture(autouse=True)
def clean_listeners():
    before = list(audio.frame_listeners)
    yield
    audio.frame_listeners[:] = before


def test_microphone_calls_frame_listeners_and_survives_a_bad_one():
    got = []
    bad_calls = []

    def bad(_f):
        bad_calls.append(1)
        raise RuntimeError("boom")

    audio.frame_listeners.extend([bad, got.append])
    mic = audio.Microphone.__new__(audio.Microphone)
    mic._bad_listeners = set()
    for _ in range(3):
        mic._emit_frame(frame())
    assert len(got) == 3 and len(bad_calls) == 3
    assert bad in mic._bad_listeners


def test_ring_returns_last_n_seconds_and_pcm_frames():
    ring = MicRing(seconds=2.0)
    for i in range(200):
        ring.push(frame(i / 1000))
    assert ring.seq == 200
    assert len(ring.last_seconds(1.0)) == 50 * audio.FRAME
    assert ring.last_seconds(1.0)[-1] == pytest.approx(0.199)
    got = ring.frames_since(190)
    assert [n for n, _, _ in got] == list(range(190, 200))
    assert len(got[0][1]) == audio.FRAME * 2 and got[0][2] is None
    assert len(ring.frames_since(0)) == 100           # 2 s ring


def test_reset_rebuilds_and_restores_simulated_flag(tmp_path):
    b = VerifierBridge(tmp_path, simulated=False, step_s=0.01)
    b._build()
    if b.simulated:
        assert b._sim is not None                               # no camera and no voice template
    else:
        assert b._voice is not None or b._face is not None      # this machine has a real verifier
    b.reset()
    assert b.simulated is False and b._sim is None and not b._built
    sim = VerifierBridge(tmp_path, simulated=True, step_s=0.01)
    sim._build()
    sim.reset()
    assert sim.simulated is True and not sim._built


class FakeVoice:
    def __init__(self):
        self.source = self.stt = self.host = None
        self.verified = 0

    def attach_audio(self, source, stt):
        self.source, self.stt = source, stt

    def bind(self, host):
        self.host = host

    async def verify(self, timeout):
        self.verified += 1
        return AuthResult(AuthTier.T2_STRONG, "voice", True, detail="voice ok")


def real_bridge(monkeypatch, tmp_path, voice):
    from driveos.biometrics import factory, verifier
    monkeypatch.setattr(factory, "build_voice_first", lambda cfg, require_enrollment=True: (voice, "ok"))
    monkeypatch.setattr(factory, "build_face_first", lambda cfg: None)
    monkeypatch.setattr(verifier.FaceFirstVerifier, "sharing",
                        classmethod(lambda cls, v: SimpleNamespace(bind=lambda h: None)))
    return VerifierBridge(tmp_path, simulated=False)


def test_real_voice_gets_the_mic_ring_and_reset_detaches(monkeypatch, tmp_path):
    voice = FakeVoice()
    b = real_bridge(monkeypatch, tmp_path, voice)
    assert b.verifier_for("voice", SimpleNamespace()) is voice
    assert b.simulated is False and b.real_methods() == ["Voice ID", "Face ID"]
    assert isinstance(voice.source, MicRing)
    for f in (frame(), frame()):
        for fn in audio.frame_listeners:
            fn(f)
    assert voice.source.seq == 2
    b.reset()
    assert voice.source.push not in audio.frame_listeners


@arun
async def test_approve_pays_by_real_voice_without_otp(home, monkeypatch):    # noqa: F811
    voice = FakeVoice()
    eng, pay = make(home)
    pay.bridge = real_bridge(monkeypatch, home, voice)
    seed_known(pay)
    await pay.begin(cart(4.0))
    card = pay.build_card()
    assert any("Voice ID" in str(r.get("value")) for r in card["rows"])
    assert not any("Simulated" in str(r.get("value")) for r in card["rows"])
    assert await pay.intercept("approve") is True
    await wait_task(pay)
    assert voice.verified == 1
    assert pay.co is None and pay.ledger.entries()[-1].kind == "captured"
    assert pay.ledger.entries()[-1].payload["method"] == "voice"
    assert eng.cards()[-1]["kind"] == "receipt"


@arun
async def test_no_template_keeps_simulated_ladder_and_says_so(home):          # noqa: F811
    _eng, pay = make(home)
    pay.bridge = VerifierBridge(home, simulated=False, step_s=0.01)
    seed_known(pay)
    await pay.begin(cart(4.0))
    if not pay.bridge.simulated:
        pytest.skip("a real camera or Voice ID is available, so checkout does not use the simulator")
    assert any("Simulated" in str(r.get("value")) for r in pay.build_card()["rows"])
    await pay.intercept("approve")
    await wait_task(pay)
    assert pay.ledger.entries()[-1].kind == "captured"
    pay.refresh_verifiers()
    assert pay.bridge.simulated is False and not pay.bridge._built
