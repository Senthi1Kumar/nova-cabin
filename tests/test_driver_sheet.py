"""Driver sheet: open/tab/close, consent, and server-side confirmation steps."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from driveos.biometrics.enroll import ConsentRequired

from cabin.driver.consent import ConsentStore
from cabin.driver.sheet import DriverSheet
from cabin.wallet import Wallet


def run(coro):
    return asyncio.run(coro)


class Engine:
    def __init__(self):
        self.action_handlers = {}
        self.events = []
        self.state = "idle"
        self.mic = None
        self.memory = None
        self.payment = None

    def emit(self, key, value):
        self.events.append((key, value))


class FakeVoice:
    def __init__(self, consent):
        self.consent = consent
        self.deleted = False
        self.started = None

    def state(self):
        return {"stage": "idle", "enrolled": True}

    async def start(self, name):
        if not self.consent.get("voice"):
            raise ConsentRequired("recording consent is required")
        self.started = name

    async def delete(self):
        self.deleted = True

    async def refresh(self):
        pass


class FakeFace:
    def __init__(self):
        self.deleted = []

    def state(self):
        return {"stage": "idle", "profiles": []}

    async def delete(self, pid):
        self.deleted.append(pid)

    async def refresh(self):
        pass


def make(tmp_path):
    eng = Engine()
    wallet = Wallet(tmp_path)
    sheet = DriverSheet(eng, wallet=wallet, voice=FakeVoice(ConsentStore(tmp_path)), face=FakeFace(), home=tmp_path)
    return eng, sheet, wallet


class RecordingVoice(FakeVoice):
    def __init__(self):
        self.stage_name = "recording"
        self.cancelled = 0

    def state(self):
        return {"stage": self.stage_name}

    async def cancel(self):
        self.cancelled += 1
        self.stage_name = "idle"


def test_close_and_tab_cancel_an_open_voice_take(tmp_path):
    eng = Engine()
    voice = RecordingVoice()
    DriverSheet(eng, wallet=Wallet(tmp_path), voice=voice, face=FakeFace(), home=tmp_path)
    run(eng.action_handlers["driver_close"]({}))
    assert voice.cancelled == 1 and eng.events[-1][1]["open"] is False
    voice.stage_name = "analyzing"
    run(eng.action_handlers["driver_tab"]({"tab": "cards"}))
    assert voice.cancelled == 2 and eng.events[-1][1]["tab"] == "cards"
    voice.stage_name = "ready"
    run(eng.action_handlers["driver_close"]({}))
    assert voice.cancelled == 2


def test_driver_sheet_state_roundtrip(tmp_path):
    eng, _sheet, _w = make(tmp_path)
    run(eng.action_handlers["driver_open"]({}))
    run(eng.action_handlers["driver_tab"]({"tab": "cards"}))
    run(eng.action_handlers["driver_close"]({}))
    assert {k for k, _ in eng.events} == {"driver"}
    opened, tabbed, closed = eng.events[1][1], eng.events[3][1], eng.events[-1][1]
    assert opened["open"] is True and opened["tab"] == "voice"
    assert tabbed["open"] is True and tabbed["tab"] == "cards"
    assert closed["open"] is False


def test_consent_is_stored_per_kind_and_gates_start(tmp_path):
    eng, sheet, _w = make(tmp_path)
    run(eng.action_handlers["driver_voice_start"]({"name": "Alex"}))
    assert sheet.voice.started is None
    state = eng.events[-1][1]["state"]
    assert state["consent"] == {"voice": False, "face": False} and state["notice"]
    run(eng.action_handlers["driver_consent"]({"kind": "voice", "value": True}))
    assert eng.events[-1][1]["state"]["consent"] == {"voice": True, "face": False}
    run(eng.action_handlers["driver_voice_start"]({"name": "Alex"}))
    assert sheet.voice.started == "Alex"


def test_delete_needs_a_confirm_step_and_cancel_drops_it(tmp_path):
    eng, sheet, _w = make(tmp_path)
    run(eng.action_handlers["driver_voice_delete"]({}))
    assert sheet.voice.deleted is False
    assert eng.events[-1][1]["state"]["pending"]["kind"] == "voice_delete"
    run(eng.action_handlers["driver_cancel"]({}))
    assert eng.events[-1][1]["state"]["pending"] is None and sheet.voice.deleted is False
    run(eng.action_handlers["driver_face_delete"]({"profile_id": "p1"}))
    assert sheet.face.deleted == []
    run(eng.action_handlers["driver_confirm"]({}))
    assert sheet.face.deleted == ["p1"] and eng.events[-1][1]["state"]["pending"] is None


def test_confirm_without_pending_does_nothing(tmp_path):
    eng, sheet, _w = make(tmp_path)
    run(eng.action_handlers["driver_confirm"]({}))
    assert sheet.voice.deleted is False and sheet.face.deleted == []


def test_card_add_form_keeps_only_four_digits(tmp_path):
    eng, _sheet, wallet = make(tmp_path)
    before = len(wallet.list())
    run(eng.action_handlers["driver_card_add"]({"brand": "visa", "last4": "4242424242424242", "expiry": "12/30"}))
    assert len(wallet.list()) == before and eng.events[-1][1]["state"]["notice"]
    run(eng.action_handlers["driver_card_add"]({"brand": "visa", "last4": "1111", "expiry": "12/30",
                                                "label": "Work Visa"}))
    cards = eng.events[-1][1]["state"]["cards"]
    assert len(cards) == before + 1 and any(c["label"] == "Work Visa" and c["last4"] == "1111" for c in cards)
    assert not any(len(c["last4"]) > 4 for c in cards)


def test_card_remove_is_confirmed(tmp_path):
    eng, _sheet, wallet = make(tmp_path)
    other = next(c for c in wallet.list() if not c.default)
    run(eng.action_handlers["driver_card_remove"]({"token": other.token}))
    assert wallet.get(other.token) is not None
    run(eng.action_handlers["driver_confirm"]({}))
    assert wallet.get(other.token) is None


def test_card_rename_and_expiry_edit(tmp_path):
    eng, _sheet, wallet = make(tmp_path)
    card = wallet.list()[0]
    run(eng.action_handlers["driver_card_edit"]({"token": card.token, "label": "Fuel", "expiry": "01/31"}))
    assert wallet.get(card.token).label == "Fuel" and wallet.get(card.token).expiry == "01/31"
    run(eng.action_handlers["driver_card_edit"]({"token": card.token, "expiry": "13-31"}))
    assert eng.events[-1][1]["state"]["notice"]


class FakeProfileStore:
    def __init__(self):
        self.p = SimpleNamespace(name="Alex", home_city="Trichy", work_city=None, preferred_places=["Blue Cafe"],
                                 usual_orders=[], interests=[], units=None, temp_pref_c=21.0, provenance={})
        self.gone = []
        self.all_forgotten = False

    def load(self):
        return self.p

    def forget(self, key):
        self.gone.append(key)
        setattr(self.p, key, None if key in ("home_city", "name") else [])
        return True

    def forget_all(self):
        self.all_forgotten = True
        for k in ("name", "home_city", "temp_pref_c"):
            setattr(self.p, k, None)
        self.p.preferred_places = []


class FakeMemory:
    def __init__(self):
        self.profile = FakeProfileStore()
        self.purged = False

    async def _refresh(self):
        pass

    async def purge(self):
        self.purged = True


def test_memory_tab_uses_engine_memory_forget_one_and_all_confirmed(tmp_path):
    eng, _sheet, _w = make(tmp_path)
    eng.memory = FakeMemory()
    run(eng.action_handlers["driver_tab"]({"tab": "memory"}))
    mem = eng.events[-1][1]["state"]["memory"]
    assert mem["available"] is True
    fields = {f["field"]: f for f in mem["fields"]}
    assert fields["home_city"]["value"] == "Trichy" and fields["preferred_places"]["value"] == "Blue Cafe"
    run(eng.action_handlers["driver_memory_forget"]({"field": "home_city"}))
    assert eng.memory.profile.gone == ["home_city"]
    assert "home_city" not in {f["field"] for f in eng.events[-1][1]["state"]["memory"]["fields"]}
    run(eng.action_handlers["driver_memory_forget_all"]({}))
    assert eng.memory.profile.all_forgotten is False
    assert eng.events[-1][1]["state"]["pending"]["kind"] == "memory_forget_all"
    run(eng.action_handlers["driver_confirm"]({}))
    assert eng.memory.profile.all_forgotten and eng.memory.purged
    assert eng.events[-1][1]["state"]["memory"]["fields"] == []


def test_memory_tab_unavailable_without_engine_memory(tmp_path):
    eng, _sheet, _w = make(tmp_path)
    run(eng.action_handlers["driver_tab"]({"tab": "memory"}))
    mem = eng.events[-1][1]["state"]["memory"]
    assert mem["available"] is False and mem["fields"] == []
