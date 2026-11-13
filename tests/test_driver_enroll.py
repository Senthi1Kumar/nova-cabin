"""Voice and face enrollment: consent, mic capture, quality gate, DriveOS template and face stores."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from driveos.biometrics.capture import Capture
from driveos.biometrics.enroll import ConsentRequired
from driveos.biometrics.f455 import F455Result, FaceUserMap
from driveos.biometrics.quality import Quality
from driveos.server.enroll import EnrollError

from cabin.driver.consent import ConsentStore
from cabin.driver.face_enroll import FaceEnroll
from cabin.driver.mic_tap import MicTap
from cabin.driver.voice_enroll import VoiceEnroll


def run(coro):
    return asyncio.run(coro)


class Engine:
    def __init__(self):
        self.events = []
        self.state = "idle"
        self.payment = None
        self.mic = None

    def emit(self, key, value):
        self.events.append((key, value))


class FakeTap:
    """Stands in for the mic tap: a ring source plus the recording gate."""
    seq = 0

    def frames_since(self, seq):
        return []

    def attach(self, mic):
        return True

    def capturing(self):
        import contextlib
        return contextlib.nullcontext()


def good_capture():
    return Capture(np.ones(16000 * 3, np.float32) * 0.1, None, True, 3.0)


class FakeSession:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.reports = []
        self.finalized = False

    def add(self, audio):
        ok, reasons = self.verdicts.pop(0)
        q = Quality(ok, 2.4 if ok else 0.8, 20.0, 0.0, reasons)
        self.reports.append(SimpleNamespace(accepted=ok, quality=q))
        return self.reports[-1]

    @property
    def accepted(self):
        return [r for r in self.reports if r.accepted]

    def finalize(self):
        self.finalized = True
        return SimpleNamespace(kept=len(self.accepted), dropped_outliers=[])


def make_voice(tmp_path, verdicts, *, consent=True, capture=None, engine=None):
    store = ConsentStore(tmp_path)
    if consent:
        store.set("voice", True)
    session = FakeSession(verdicts)
    changes = []

    async def factory(name):
        return session

    async def cap(_ring):
        return good_capture()

    voice = VoiceEnroll(engine or Engine(), home=tmp_path, consent=store, tap=FakeTap(),
                        session_factory=factory, capture=capture or cap, on_change=lambda: changes.append(1))
    return voice, session, changes


def take(voice):
    async def go():
        task = voice.take()
        await task
    run(go())


def test_voice_start_needs_stored_consent(tmp_path):
    voice, _s, _c = make_voice(tmp_path, [], consent=False)
    with pytest.raises(ConsentRequired):
        run(voice.start("Alex"))
    assert voice.state()["stage"] == "idle"


def test_voice_low_quality_take_shows_reason_and_keeps_step(tmp_path):
    voice, session, _c = make_voice(tmp_path, [(False, ["too noisy (SNR 3 dB)"])])

    async def go():
        await voice.start("Alex")
        before = voice.state()["step"]
        await voice.take()
        return before

    before = run(go())
    st = voice.state()
    assert st["step"] == before and st["kept"] == 0 and st["stage"] == "ready"
    assert "too noisy" in st["message"]
    assert st["last"]["ok"] is False and not session.finalized


def test_voice_good_takes_advance_then_finalize_writes_template_via_session(tmp_path):
    voice, session, _c = make_voice(tmp_path, [(True, []), (True, []), (True, [])])

    async def go():
        await voice.start("Alex")
        steps = []
        for _ in range(3):
            await voice.take()
            steps.append(voice.state()["step"])
        return steps

    steps = run(go())
    st = voice.state()
    assert steps[0] == 2 and st["done"] is True and st["enrolled"] is True and st["stage"] == "done"
    assert session.finalized and st["kept"] == 3


def test_voice_unheard_take_is_retried(tmp_path):
    async def silent(_ring):
        return Capture(np.zeros(0, np.float32), None, False, 0.0)

    voice, _session, _c = make_voice(tmp_path, [], capture=silent)

    async def go():
        await voice.start("Alex")
        await voice.take()

    run(go())
    st = voice.state()
    assert st["stage"] == "ready" and "hear" in st["message"] and st["kept"] == 0


def test_voice_take_refused_while_assistant_busy(tmp_path):
    eng = Engine()
    voice, _s, _c = make_voice(tmp_path, [(True, [])], engine=eng)

    async def go():
        await voice.start("Alex")
        eng.state = "speaking"
        with pytest.raises(EnrollError):
            await voice.take()

    run(go())


def test_voice_real_session_writes_template_where_driveauth_reads_it(tmp_path, monkeypatch):
    """Real EnrollmentSession + TemplateStore, fake embedder: the .tpl lands in <home>/voice/<driver>.tpl."""
    import driveos.biometrics.enroll as de

    monkeypatch.setattr(de, "assess", lambda audio: Quality(True, 3.0, 25.0, 0.0, []))

    class Embedder:
        name = "fake"

        def embed(self, audio):
            v = np.zeros(8, np.float32)
            v[0] = 1.0
            v[1] = float(len(audio) % 7) * 0.001
            return v / np.linalg.norm(v)

    voice = VoiceEnroll(Engine(), home=tmp_path, consent=ConsentStore(tmp_path), tap=FakeTap(),
                        embedder_factory=lambda cfg: Embedder(), capture=lambda _r: _async(good_capture()))
    ConsentStore(tmp_path).set("voice", True)

    async def go():
        await voice.start("Alex")
        for _ in range(3):
            await voice.take()

    run(go())
    assert voice.state()["done"] is True
    assert (tmp_path / "voice" / "driver1.tpl").is_file()
    run(voice.refresh())
    assert voice.state()["enrolled"] is True and voice.state()["name"] == "Alex"
    run(voice.rename("Sam"))
    assert voice.state()["name"] == "Sam"
    run(voice.delete())
    assert not (tmp_path / "voice" / "driver1.tpl").exists() and voice.state()["enrolled"] is False


async def _async(value):
    return value


def test_mic_tap_gates_engine_frames_only_while_recording():
    seen = []
    mic = SimpleNamespace(on_frame=seen.append, available=True)
    tap = MicTap()
    assert tap.attach(mic) is True
    mic.on_frame(np.zeros(320, np.float32))
    assert len(seen) == 1 and tap.seq == 0
    with tap.capturing():
        mic.on_frame(np.full(320, 0.5, np.float32))
        mic.on_frame(np.full(320, 0.5, np.float32))
    mic.on_frame(np.zeros(320, np.float32))
    assert len(seen) == 2 and tap.seq == 2
    frames = tap.frames_since(0)
    assert [s for s, _a, _b in frames] == [0, 1]
    assert len(frames[0][1]) == 640 and frames[0][2] is None
    assert tap.frames_since(2) == []
    assert tap.attach(SimpleNamespace(on_frame=None, available=False)) is False


# face ----------------------------------------------------------------------------------------------------------

class FakeF455:
    hub = None

    def __init__(self, present=True, results=None):
        self.present = present
        self.results = list(results or [])
        self.removed = []
        self.auth = F455Result()
        self.checked = 0

    def device_present(self):
        self.checked += 1
        return self.present

    def enroll(self, uid, timeout_s=30.0):
        return self.results.pop(0)

    def authenticate(self, timeout_s):
        return self.auth

    def remove_user(self, uid):
        self.removed.append(uid)
        return True

    def cancel(self):
        pass


def make_face(tmp_path, client, *, consent=True):
    store = ConsentStore(tmp_path)
    if consent:
        store.set("face", True)
    changes = []
    face = FaceEnroll(Engine(), home=tmp_path, consent=store, client=client, on_change=lambda: changes.append(1))
    return face, changes


async def settle(face):
    for _ in range(20):
        await asyncio.sleep(0.01)
        if face.state()["stage"] not in ("capturing", "deleting"):
            return


def test_face_without_camera_is_degraded(tmp_path):
    face, _c = make_face(tmp_path, FakeF455(present=False))

    async def go():
        await face.refresh()
        await face.start("Alex")

    run(go())
    st = face.state()
    assert st["degraded"] is True and "camera" in st["message"].lower()
    assert face.engine.events[-1][1]["degraded"] is True


def test_face_start_needs_consent(tmp_path):
    face, _c = make_face(tmp_path, FakeF455(), consent=False)
    with pytest.raises(ConsentRequired):
        run(face.start("Alex"))


def test_face_enroll_select_rename_delete_roundtrip(tmp_path):
    client = FakeF455(results=[F455Result(matched=True, user_id="u1", status="Success")])
    face, _c = make_face(tmp_path, client)

    async def go():
        await face.refresh()
        await face.start("Alex", "new")
        await settle(face)
        await face.refresh()

    run(go())
    st = face.state()
    assert st["stage"] == "done" and len(st["profiles"]) == 1
    pid = st["profiles"][0]["id"]
    assert st["selected"] == pid and st["profiles"][0]["name"] == "Alex"
    run(face.rename(pid, "Sam"))
    assert FaceUserMap(tmp_path).get_name(pid) == "Sam"
    run(face.select(pid))
    client.results.append(F455Result(matched=True, user_id="u2", status="Success"))

    async def reenroll():
        await face.start("Sam", pid)     # re-enroll the same profile
        await settle(face)
        assert face.state()["stage"] == "done"
        face.dismiss()

    run(reenroll())

    async def delete():
        await face.delete(pid)
        await settle(face)
        await face.refresh()

    run(delete())
    assert face.state()["profiles"] == [] and client.removed


def test_face_duplicate_goes_to_link_confirm_then_links(tmp_path):
    client = FakeF455(results=[F455Result(matched=False, status="DuplicateFaceprints")])
    client.auth = F455Result(matched=True, user_id="existing", status="Success")
    face, _c = make_face(tmp_path, client)

    async def go():
        await face.start("Alex", "new")
        await settle(face)
        assert face.state()["stage"] == "link_confirm"
        await face.link()
        await settle(face)
        await face.refresh()

    run(go())
    st = face.state()
    assert st["stage"] == "done" and st["profiles"][0]["linked"] is True
