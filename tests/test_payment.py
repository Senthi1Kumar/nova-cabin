"""Payment: wallet, checkout card, DriveAuth ladder (simulated), intercepts and ledger."""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cabin.config import Settings
from cabin.payment import Payment, spoken_digits
from cabin.services.driveauth_bridge import VerifierBridge
from cabin.wallet import Wallet

PAN_LIKE = re.compile(r"\d{13,19}")


def arun(fn):
    """Run an async test body on a fresh loop (no pytest-asyncio in the venv)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return wrapper


def cart(total: float = 8.5, place_id: str = "p_blue") -> dict:
    return {"lines": [{"item_id": "i1", "name": "Latte", "qty": 2, "unit_price": total / 2, "options": {}}],
            "total": total, "currency": "USD", "place": {"id": place_id, "name": "Blue Bottle"}}


class FakeTools:
    def __init__(self):
        self.schemas, self.methods, self.captions = [], {}, {}

    def extend(self, schemas, methods, captions=None):
        self.schemas.extend(schemas)
        self.methods.update(methods)
        self.captions.update(captions or {})


class FakeVehicle:
    lat, lon = 37.78, -122.42


class FakeEngine:
    def __init__(self, sim: bool = True, **raw):
        self.cfg = Settings(raw={"PAY_SIM_STEP_S": "0.01", **raw})
        self.sim = sim
        self.state = "idle"
        self.events: list[tuple[str, object]] = []
        self.spoken: list[str] = []
        self.action_handlers, self.intercepts, self.system_extra = {}, [], []
        self.tools = FakeTools()
        self.vehicle = FakeVehicle()
        self.last_card = None

    def emit(self, key, value):
        self.events.append((key, value))

    def _set_state(self, s, caption=None):
        self.state = s

    async def _speak_loop(self, q):
        while (item := await q.get()) is not None:
            self.spoken.append(item)

    def cards(self):
        return [v for k, v in self.events if k == "card" and v]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVA_CABIN_DATA", str(tmp_path))
    return tmp_path


def make(home, **raw):
    eng = FakeEngine(**raw)
    pay = Payment(eng)
    pay.install()
    return eng, pay


def seed_known(pay, place_id: str = "p_blue") -> None:
    """A prior captured order makes the merchant a known beneficiary, so DriveAuth does not ask for a code."""
    pay.ledger.upsert_transaction("tx_seed", state="captured", occupant_id="driver1", payer_id="driver1",
                                  merchant_id=place_id, provider="nova-cabin", total_minor=100, currency="USD",
                                  tier="T2_STRONG", snapshot={})


async def settle(pay, stage: str = "otp", timeout: float = 5.0):
    async def wait():
        while pay.co is None or pay.co.stage != stage:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), timeout)


async def wait_task(pay):
    co = pay.co
    if co is not None and co.task is not None:
        await asyncio.wait_for(asyncio.shield(co.task), 10)


# wallet --------------------------------------------------------------------------
def test_wallet_round_trip_and_encryption(home):
    w = Wallet(home)
    assert [c.label for c in w.cards()] == ["Visa ••4242", "Mastercard ••5555"]
    assert w.default().brand == "Visa"
    raw = w.path.read_bytes()
    assert b"4242" not in raw and b"Visa" not in raw
    assert stat.S_IMODE(w.key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(w.path.stat().st_mode) == 0o600
    w.add("amex", "0005", "01/30")
    w2 = Wallet(home)
    assert [c.last4 for c in w2.cards()] == ["4242", "5555", "0005"]
    assert w2.by_brand("master card").last4 == "5555"
    assert all(c.token.startswith("tok_") for c in w2.cards())


def test_wallet_refuses_full_numbers(home):
    w = Wallet(home)
    with pytest.raises(ValueError):
        w.add("visa", "4111111111111111", "01/30")


# tool + checkout card --------------------------------------------------------------
@arun
async def test_list_cards_tool_and_registration(home):
    eng, pay = make(home)
    assert eng.payment is pay
    assert "pay_list_cards" in eng.tools.methods
    result, card, nav = await eng.tools.methods["pay_list_cards"]()
    assert [c["last4"] for c in result["cards"]] == ["4242", "5555"]
    assert card["kind"] == "wallet" and nav is None
    assert set(eng.action_handlers) == {"approve_voice", "approve_face", "swap_card", "cancel"}


@arun
async def test_begin_builds_checkout_card(home):
    eng, pay = make(home)
    out = await pay.begin(cart())
    assert out["status"] == "awaiting_approval" and out["card"] == "Visa ••4242"
    card = eng.cards()[-1]
    assert card["kind"] == "checkout"
    assert [a["key"] for a in card["actions"]] == ["approve_voice", "approve_face", "swap_card", "cancel"]
    texts = " | ".join(r["label"] + " " + r["value"] for r in card["rows"])
    assert "2 × Latte" in texts and "$8.50" in texts and "Visa ••4242" in texts
    assert any("checkout" in line.lower() for line in eng.system_extra)
    snap = [v for k, v in eng.events if k == "checkout"][-1]
    assert snap["stage"] == "review" and snap["card"]["last4"] == "4242" and snap["otp_pending"] is False
    assert eng.spoken and "8" in eng.spoken[0]


@arun
async def test_begin_rejects_empty_cart(home):
    eng, pay = make(home)
    out = await pay.begin({"lines": [], "total": 0, "currency": "USD", "place": {"id": "x", "name": "X"}})
    assert out["status"] == "empty_cart" and pay.co is None and not eng.system_extra


# ladder ----------------------------------------------------------------------------
@arun
async def test_voice_accept_captures_and_writes_ledger(home):
    eng, pay = make(home)
    seed_known(pay)
    await pay.begin(cart(4.0))
    assert await pay.intercept("approve") is True
    await wait_task(pay)
    assert pay.co is None and not eng.system_extra
    assert eng.cards()[-1]["kind"] == "receipt"
    entries = pay.ledger.entries()
    assert [e.kind for e in entries] == ["authorised", "captured"]
    assert entries[-1].payload["total_minor"] == 400
    assert pay.ledger.verify() == 2
    assert any("Paid" in s for s in eng.spoken)


@arun
async def test_voice_fails_then_face_accepts(home):
    _eng, pay = make(home, PAY_SIM_VOICE="impostor", PAY_SIM_FACE="accept")
    seed_known(pay)
    await pay.begin(cart(4.0))
    await pay.intercept("approve")
    await wait_task(pay)
    assert pay.co is None
    assert pay.ledger.entries()[-1].kind == "captured"
    assert "face" in pay.ledger.entries()[-1].payload["rule"]


@arun
async def test_face_action_skips_voice(home):
    eng, pay = make(home, PAY_SIM_VOICE="accept", PAY_SIM_FACE="accept")
    seed_known(pay)
    await pay.begin(cart(4.0))
    await eng.action_handlers["approve_face"]({})
    await wait_task(pay)
    assert pay.ledger.entries()[-1].payload["method"] == "face"


@arun
async def test_rejected_returns_to_review_then_locks(home):
    eng, pay = make(home, PAY_SIM_VOICE="impostor", PAY_SIM_FACE="impostor")
    seed_known(pay)
    await pay.begin(cart(4.0))
    await pay.intercept("approve")
    await wait_task(pay)
    assert pay.co is not None and pay.co.stage == "review" and pay.co.attempts == 1
    assert eng.cards()[-1]["actions"][0]["key"] == "approve_voice"
    assert pay.ledger.entries() == []
    for _ in range(2):
        await pay.intercept("approve")
        await wait_task(pay)
    assert pay.co is None and not eng.system_extra
    assert any("Too many" in s for s in eng.spoken)


@arun
async def test_otp_step_up_with_spoken_digits(home):
    eng, pay = make(home)
    await pay.begin(cart(400.0, place_id="p_new"))
    await pay.intercept("approve")
    await settle(pay)
    assert pay.co.stage == "otp"
    snap = [v for k, v in eng.events if k == "checkout"][-1]
    assert snap["otp_pending"] is True
    code = pay.co.phone_note
    assert len(code) == 6
    wrong = "".join(str((int(d) + 1) % 10) for d in code)
    assert await pay.intercept(" ".join(wrong)) is True
    assert pay.co.stage == "otp" and pay.co.otp.attempts_left == 2
    assert await pay.intercept("12") is True            # partial: re-prompt, no attempt used
    assert pay.co.otp.attempts_left == 2
    assert await pay.intercept("what's the weather") is True   # nothing reaches the model while a code is awaited
    assert pay.co.stage == "otp" and pay.co.otp.attempts_left == 2
    mis = {"0": "o", "1": "won", "2": "do", "3": "tree", "4": "for", "5": "fire", "6": "sick",
           "7": "sound", "8": "egg", "9": "night"}
    assert await pay.intercept("it is " + " ".join(mis[d] for d in code[:3])) is True   # short: re-prompt
    assert pay.co.otp.attempts_left == 2
    words = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six",
             "7": "seven", "8": "eight", "9": "nine"}
    assert await pay.intercept(" ".join(words[d] for d in code)) is True
    await wait_task(pay)
    assert pay.co is None
    assert pay.ledger.entries()[-1].kind == "captured"


@arun
async def test_otp_lockout_declines(home):
    _eng, pay = make(home)
    await pay.begin(cart(400.0, place_id="p_new"))
    await pay.intercept("approve")
    await settle(pay)
    code = pay.co.phone_note
    wrong = "".join(str((int(d) + 1) % 10) for d in code)
    for _ in range(3):
        await pay.intercept(wrong)
    await wait_task(pay)
    assert pay.co is not None and pay.co.attempts == 1 and pay.ledger.entries() == []


# intercepts -------------------------------------------------------------------------
@arun
async def test_intercepts_ignored_without_checkout(home):
    _eng, pay = make(home)
    for phrase in ("approve", "cancel", "pay with my Mastercard", "1 2 3 4"):
        assert await pay.intercept(phrase) is False


@arun
async def test_swap_card_by_brand_and_other(home):
    eng, pay = make(home)
    await pay.begin(cart())
    assert await pay.intercept("pay with my Mastercard") is True
    assert pay._active_card().last4 == "5555"
    assert "Mastercard ••5555" in eng.cards()[-1]["subtitle"]
    assert await pay.intercept("use my other card") is True
    assert pay._active_card().last4 == "4242"
    assert await pay.intercept("use my visa") is True
    assert pay._active_card().last4 == "4242"
    await eng.action_handlers["swap_card"]({})
    assert pay._active_card().last4 == "5555"


@arun
async def test_cancel_phrase_and_action(home):
    eng, pay = make(home)
    await pay.begin(cart())
    assert await pay.intercept("Cancel.") is True
    assert pay.co is None and not eng.system_extra
    assert eng.cards()[-1]["title"] == "Checkout cancelled"
    await pay.begin(cart())
    await eng.action_handlers["cancel"]({})
    assert pay.co is None
    assert pay.ledger.entries() == []


@arun
async def test_unrelated_speech_reaches_the_model(home):
    _eng, pay = make(home)
    await pay.begin(cart())
    assert await pay.intercept("what's the weather in Reno") is False
    assert await pay.intercept("how many calories in a latte") is False


def test_spoken_digit_parsing():
    assert spoken_digits("four two seven one") == "4271"
    assert spoken_digits("4 2 7 1 8 oh") == "427180"
    assert spoken_digits("my code is 4271") is None
    assert spoken_digits("hello") is None


def test_spoken_digits_lenient_repairs_misheard_code():
    assert spoken_digits("Fire one three sound egg do.", lenient=True) == "513782"
    assert spoken_digits("zero five summer five eight one", lenient=True) == "057581"
    assert spoken_digits("It is five six four zero two nine.", lenient=True) == "564029"
    assert spoken_digits("the code is 4 2 7 1 8 oh", lenient=True) == "427180"
    assert spoken_digits("sevon tree nein", lenient=True) == "739"
    assert spoken_digits("cancel that", lenient=True) is None


# secrets hygiene -----------------------------------------------------------------------
@arun
async def test_no_pan_in_state_or_logs(home, caplog):
    caplog.set_level(logging.DEBUG)
    eng, pay = make(home)
    seed_known(pay)
    await pay.begin(cart(4.0))
    await pay.intercept("approve")
    await wait_task(pay)
    blob = json.dumps([[k, v] for k, v in eng.events], default=str) + json.dumps(eng.spoken)
    blob += "\n".join(r.getMessage() for r in caplog.records)
    blob += json.dumps([e.payload for e in pay.ledger.entries()])
    assert not PAN_LIKE.search(blob)
    assert "tok_" not in blob


def test_bridge_falls_back_to_sim_without_devices(home, monkeypatch):
    from driveos.biometrics import factory
    monkeypatch.setattr(factory, "build_voice_first", lambda cfg, **kw: (None, "no enrollment"))
    monkeypatch.setattr(factory, "build_face_first", lambda cfg, **kw: None)
    b = VerifierBridge(home, simulated=False, step_s=0.01)
    b._build()
    assert b.simulated is True
