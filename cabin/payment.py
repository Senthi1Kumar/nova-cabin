"""Payment: checkout card, saved-card wallet, DriveAuth approval ladder and one-time codes.

``install(engine)`` sets ``engine.payment``. The commerce module hands over a cart with
``await engine.payment.begin(cart_snapshot)``; from there the checkout lives on one generic assistant card
(lines, total, active card, approve / swap / cancel actions) and a set of turn intercepts, so "approve",
"cancel", "pay with my Mastercard" and spoken one-time codes are handled without reaching the language model.

The approval decision belongs to DriveAuth (voice, then face, then a one-time code) through
``cabin.services.driveauth_bridge``. Only tokens, brand and last four digits exist in this process.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from .services.driveauth_bridge import VerifierBridge
from .services.face_preview import face_caption
from .wallet import SavedCard, Wallet, data_dir, normalise_brand

log = logging.getLogger("payment")
DRIVE_ASK_TTL_S = 30.0

OCCUPANT = "driver1"
PHONE = "+10000000000"
VERIFY_TIMEOUT_S = 180.0
MAX_ATTEMPTS = 3
OTP_LENGTH = 6

APPROVE_RE = re.compile(
    r"^(?:yes|yeah|yep|ok|okay|sure|approve|confirm|go ahead|pay|pay now|do it|approve it|approve payment|"
    r"confirm payment|yes please|please do|approve with voice(?: id)?|use voice(?: id)?|voice id)$")
FACE_RE = re.compile(r"^(?:approve with face(?: id)?|use (?:my )?face(?: id)?|face id|face|pay with face(?: id)?)$")
CANCEL_RE = re.compile(
    r"^(?:cancel|cancel (?:it|that|checkout|the order|the payment|payment|my order)|never ?mind|stop|forget it|"
    r"don'?t pay|abort|no|nope|no thanks)$")
SWAP_RE = re.compile(
    r"\b(?:pay with|use|switch to|charge|put it on)\b.*\b(master ?card|visa|other card|another card|"
    r"different card|my other card)\b|\b(?:change|swap|switch) (?:the |my )?card\b")
GREETING_RE = re.compile(r"^(?:hey |ok |okay )?(?:nova|aria)[, ]+")
DRIVE_YES_RE = re.compile(
    r"^(?:yes|yeah|yep|sure|ok|okay|drive there|drive|let'?s go|take me there|yes drive there)$")
DRIVE_NO_RE = re.compile(r"^(?:no|nope|not now|no thanks|don't|do not)$")

DIGIT_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
               "six": "6", "seven": "7", "eight": "8", "nine": "9"}
# How on-device ASR tends to mishear a digit spoken on its own. Used only while a code is awaited.
OTP_HOMOPHONES = {
    "o": "0", "nil": "0", "ought": "0", "won": "1", "wan": "1", "to": "2", "too": "2", "do": "2", "tu": "2",
    "tree": "3", "free": "3", "for": "4", "fore": "4", "fire": "5", "fife": "5", "hive": "5", "sex": "6",
    "sick": "6", "sticks": "6", "sound": "7", "summer": "7", "sever": "7", "heaven": "7", "egg": "8",
    "ate": "8", "hate": "8", "night": "9", "nein": "9", "mine": "9"}
OTP_FILLER = {"it", "its", "it's", "is", "the", "code", "my", "otp", "pin", "number", "here", "this",
              "that", "um", "uh", "and", "a", "please", "nova", "hey", "ok", "okay", "yes"}


def normalise(text: str) -> str:
    t = re.sub(r"[^\w\s'\-]", " ", text.lower())
    t = re.sub(r"\s+", " ", t).strip()
    return GREETING_RE.sub("", t).strip()


def spoken_digits(text: str, lenient: bool = False) -> str | None:
    """The digit string when ``text`` is nothing but digits or digit words; otherwise None.

    ``lenient`` (a code is awaited) also skips filler ("the code is"), maps common
    mishearings of single digits and fuzzy-matches the rest against the digit words."""
    out = ""
    for tok in re.split(r"[\s\-]+", normalise(text)):
        if not tok:
            continue
        if tok.isdigit():
            out += tok
        elif tok in DIGIT_WORDS:
            out += DIGIT_WORDS[tok]
        elif not lenient:
            return None
        elif tok in OTP_HOMOPHONES:
            out += OTP_HOMOPHONES[tok]
        elif tok in OTP_FILLER:
            continue
        else:
            close = difflib.get_close_matches(tok, DIGIT_WORDS, n=1, cutoff=0.75)
            if close:
                out += DIGIT_WORDS[close[0]]
    return out or None


def money(amount: float, currency: str = "USD") -> str:
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency.upper())
    return f"{symbol}{amount:,.2f}" if symbol else f"{amount:,.2f} {currency.upper()}"


def money_words(amount: float, currency: str = "USD") -> str:
    whole, cents = divmod(round(amount * 100), 100)
    unit = "dollars" if currency.upper() == "USD" else currency.upper()
    return f"{whole} {unit}" + (f" {cents}" if cents else "")


@dataclass
class Checkout:
    id: str
    cart: dict
    card_token: str
    stage: str = "review"            # review | verifying | otp | done | closed
    method: str = ""                 # voice | face
    detail: str = ""
    attempts: int = 0
    task: asyncio.Task | None = None
    verifier: Any = None
    otp: Any = None
    waiter: asyncio.Future | None = None
    phone_note: str = ""
    risk: dict = field(default_factory=dict)
    closed: bool = False

    @property
    def total(self) -> float:
        return float(self.cart.get("total") or 0.0)

    @property
    def currency(self) -> str:
        return str(self.cart.get("currency") or "USD")

    @property
    def place_name(self) -> str:
        return str((self.cart.get("place") or {}).get("name") or "your order")


class PaymentHost:
    """What the DriveOS verifier needs from the application (its ``Host`` protocol)."""

    def __init__(self, pay: Payment):
        self.pay = pay

    async def announce(self, text: str) -> None:
        await self.pay.say(text)

    def set_capturing(self, on: bool) -> None:
        return None

    def set_status(self, stage: str, challenge: str | None, detail: str) -> None:
        co = self.pay.co
        if co is not None and stage != "otp":
            co.detail = detail
            self.pay.render()

    def set_risk(self, risk: dict) -> None:
        if self.pay.co is not None:
            self.pay.co.risk = dict(risk)

    def payment_context(self) -> dict[str, Any]:
        co = self.pay.co
        assert co is not None
        place = co.cart.get("place") or {}
        ctx: dict[str, Any] = {"amount": co.total, "merchant": co.place_name,
                               "known": self.pay.known_merchant(str(place.get("id") or ""))}
        v = getattr(self.pay.engine, "vehicle", None)
        if v is not None:
            ctx["vehicle"] = {"lat": v.lat, "lon": v.lon, "speed_kmh": 0.0, "parked": True}
        if place.get("lat") is not None and place.get("lon") is not None:
            ctx["merchant_location"] = {"lat": float(place["lat"]), "lon": float(place["lon"])}
        return ctx

    async def otp_roundtrip(self, timeout_s: float) -> bool:
        return await self.pay.otp_roundtrip(timeout_s)

    async def hold_face_result(self, states: tuple[str, ...] = ("verified", "failed")) -> None:
        """Keep the camera result on the card before a one-time code replaces it, as DriveOS does."""
        co = self.pay.co
        state = face_caption()["state"]
        if co is None or co.method != "face" or state not in states:
            return
        self.pay.render()
        await asyncio.sleep(1.2)


class Payment:
    def __init__(self, engine, wallet: Wallet | None = None, bridge: VerifierBridge | None = None):
        self.engine = engine
        cfg = engine.cfg
        home = data_dir() if wallet is None else wallet.dir
        self.wallet = wallet or Wallet(home)
        self.bridge = bridge or VerifierBridge(
            home, simulated=bool(getattr(engine, "sim", False)),
            sim_voice=cfg.get("PAY_SIM_VOICE", "accept"), sim_face=cfg.get("PAY_SIM_FACE", "accept"),
            step_s=cfg.num("PAY_SIM_STEP_S", 0.9), stt=lambda: getattr(engine, "stt", None))
        self.host = PaymentHost(self)
        self.co: Checkout | None = None
        self._drive: dict | None = None
        self._drive_at = 0.0
        self._extra_line = ""
        self._lock: asyncio.Lock | None = None
        self._ledger = None
        self._otp = None

    def refresh_verifiers(self) -> None:
        """Rebuild the Voice ID / Face ID verifiers on the next checkout (call after an enrollment)."""
        self.bridge.reset()

    # setup -------------------------------------------------------------------
    @property
    def otp(self):
        if self._otp is None:
            from driveos.wallet.auth import OtpService, PhoneNotificationDelivery
            self._otp = OtpService(PhoneNotificationDelivery(self._phone_notify), length=OTP_LENGTH,
                                   max_attempts=MAX_ATTEMPTS)
        return self._otp

    def _phone_notify(self, code: str, purpose: str) -> None:
        if self.co is not None:
            self.co.phone_note = code
        self.engine.emit("notice", {"code": code, "purpose": purpose})

    @property
    def ledger(self):
        if self._ledger is None:
            from driveos.core.keys import FileKeyStore
            from driveos.wallet.ledger import Ledger
            self._ledger = Ledger(self.wallet.dir / "ledger.db", FileKeyStore(self.wallet.dir / "ledger.key"))
        return self._ledger

    def known_merchant(self, merchant_id: str) -> bool:
        try:
            return bool(merchant_id) and self.ledger.has_completed_with(OCCUPANT, merchant_id)
        except Exception:                                            # noqa: BLE001
            return False

    def install(self) -> None:
        eng = self.engine
        eng.payment = self
        eng.tools.extend(
            [{"type": "function", "function": {
                "name": "pay_list_cards",
                "description": "List the payment cards saved in the wallet (brand and last four digits only) "
                               "and which one is active for checkout.",
                "parameters": {"type": "object", "properties": {}}}}],
            {"pay_list_cards": self.t_pay_list_cards},
            {"pay_list_cards": "Checking your cards"})
        for key, fn in (("approve_voice", self._act_voice), ("approve_face", self._act_face),
                        ("swap_card", self._act_swap), ("cancel", self._act_cancel)):
            eng.action_handlers[key] = fn
        eng.intercepts.append(self.intercept)

    # tool --------------------------------------------------------------------
    async def t_pay_list_cards(self) -> tuple[dict, dict | None, dict | None]:
        active = self._active_card()
        cards = self.wallet.cards()
        result = {"cards": [{"brand": c.brand, "last4": c.last4, "expiry": c.expiry, "default": c.default,
                             "active": c.token == active.token} for c in cards],
                  "checkout_open": self.co is not None}
        rows = [{"icon": "send", "label": c.brand, "value": f"••{c.last4} · exp {c.expiry}"
                 + (" · active" if c.token == active.token else ""),
                 "tint": "#8EF0C4" if c.token == active.token else ""} for c in cards]
        card = {"kind": "wallet", "title": "Your cards", "subtitle": f"{active.label} is the default for checkout",
                "rows": rows, "actions": []}
        return result, card, None

    def _active_card(self) -> SavedCard:
        if self.co is not None:
            c = self.wallet.get(self.co.card_token)
            if c is not None:
                return c
        return self.wallet.default()

    # checkout ----------------------------------------------------------------
    async def begin(self, cart_snapshot: dict) -> dict:
        lines = list(cart_snapshot.get("lines") or [])
        if not lines or float(cart_snapshot.get("total") or 0) <= 0:
            return {"status": "empty_cart", "message": "There is nothing to pay for yet."}
        if self.co is not None:
            await self._close(self.co, announce=False)
        if self._lock is None:
            self._lock = asyncio.Lock()
        self.co = co = Checkout(id="co_" + secrets.token_hex(6), cart=dict(cart_snapshot),
                                card_token=self.wallet.default().token)
        self._set_system_line(co)
        self.render()
        idle = getattr(self.engine, "state", "idle") == "idle"
        if idle:
            await self.say(f"Your total is {money_words(co.total, co.currency)} at {co.place_name}, "
                           f"paying with your {self._active_card().spoken}. Say approve, or use Face ID.")
        return {"status": "awaiting_approval", "total": round(co.total, 2), "currency": co.currency,
                "card": self._active_card().label,
                "instruction": "A checkout card is on screen. Tell the driver the total and card, and ask them "
                               "to say approve or use Face ID. Do not repeat the cart."}

    def _set_system_line(self, co: Checkout | None) -> None:
        if self._extra_line in self.engine.system_extra:
            self.engine.system_extra.remove(self._extra_line)
        self._extra_line = ""
        if co is not None:
            self._extra_line = (f"A checkout for {money(co.total, co.currency)} at {co.place_name} is open and "
                                "waiting for the driver's approval. Approval, cancellation, card changes and "
                                "one-time codes are handled outside the conversation; never ask for or repeat "
                                "card numbers or codes.")
            self.engine.system_extra.append(self._extra_line)

    # rendering ---------------------------------------------------------------
    def snapshot(self) -> dict:
        co = self.co
        if co is None:
            return {"stage": "closed"}
        card = self._active_card()
        return {"id": co.id, "stage": co.stage, "method": co.method, "detail": co.detail,
                "card": {"brand": card.brand, "last4": card.last4, "expiry": card.expiry},
                "total": round(co.total, 2), "currency": co.currency, "place": co.place_name,
                "otp_pending": co.stage == "otp" and co.waiter is not None,
                "otp_attempts_left": getattr(co.otp, "attempts_left", MAX_ATTEMPTS) if co.otp else MAX_ATTEMPTS,
                "attempts": co.attempts, "simulated": bool(self.bridge.simulated)}

    def build_card(self) -> dict:
        co = self.co
        assert co is not None
        card = self._active_card()
        rows = []
        for ln in co.cart.get("lines", []):
            qty = int(ln.get("qty") or 1)
            rows.append({"icon": "sparkle", "label": f"{qty} × {ln.get('name', 'Item')}",
                         "value": money(float(ln.get("unit_price") or 0) * qty, co.currency)})
        rows.append({"icon": "flag", "label": "Total", "value": money(co.total, co.currency), "tint": "#FFD27A"})
        rows.append({"icon": "user", "label": "Pay with", "value": f"{card.label} · exp {card.expiry}"})
        sub = f"{co.place_name} · {money(co.total, co.currency)} · {card.label}"
        if co.stage == "verifying":
            rows.append({"icon": "mic" if co.method == "voice" else "user",
                         "label": "Voice ID" if co.method == "voice" else "Face ID",
                         "value": co.detail or "Checking", "tint": "#9FA8FF"})
        elif co.stage == "otp":
            left = getattr(co.otp, "attempts_left", MAX_ATTEMPTS)
            rows.append({"icon": "phone", "label": "One-time code",
                         "value": f"Say or type the {OTP_LENGTH}-digit code · {left} tries left", "tint": "#9FA8FF"})
            rows.append({"icon": "bell", "label": "Phone notification",
                         "value": "Tap the bell at the top right"})
        elif co.detail:
            rows.append({"icon": "bell", "label": "Status", "value": co.detail, "tint": "#FFB25E"})
        real = self.bridge.real_methods()
        if self.bridge.simulated:
            rows.append({"icon": "pulse", "label": "Verification", "value": "Simulated (no camera or voice model)"})
        elif real:
            rows.append({"icon": "pulse", "label": "Verification", "value": " and ".join(real) + " (on this device)"})
        busy = co.stage in ("verifying", "otp")
        actions = [] if busy else [
            {"key": "approve_voice", "text": "Approve with Voice ID", "icon": "mic", "primary": True},
            {"key": "approve_face", "text": "Approve with Face ID", "icon": "user"},
            {"key": "swap_card", "text": "Use other card", "icon": "send"}]
        actions.append({"key": "cancel", "text": "Cancel", "icon": "close"})
        card = {"kind": "checkout", "title": f"Checkout · {co.place_name}", "subtitle": sub, "rows": rows,
                "actions": actions}
        if co.method == "face" and co.stage == "verifying":
            cap = face_caption()
            card["hero"] = {"kind": "face", "source": "image://facepreview/frame",
                            "label": cap["line"], "state": cap["state"]}
        return card

    def render(self) -> None:
        if self.co is None:
            return
        card = self.build_card()
        self.engine.last_card = card
        self.engine.emit("card", card)
        self.engine.emit("checkout", self.snapshot())

    async def say(self, text: str) -> None:
        eng = self.engine
        eng.emit("reply", text)
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            try:
                q: asyncio.Queue = asyncio.Queue()
                q.put_nowait(text)
                q.put_nowait(None)
                await eng._speak_loop(q)
            except Exception:
                log.exception("could not speak")
            finally:
                if getattr(eng, "state", "") == "speaking":
                    eng._set_state("idle", "")

    # approval ----------------------------------------------------------------
    def start_approval(self, method: str) -> bool:
        co = self.co
        if co is None:
            return False
        if co.task is not None and not co.task.done():
            asyncio.ensure_future(self.say("I'm already checking. One moment."))
            return True
        co.task = asyncio.ensure_future(self._verify(co, method))
        return True

    def _watch_face(self, method: str):
        """Redraw the checkout card when the camera hub changes, on the Qt thread's event loop."""
        if method != "face":
            return lambda: None
        try:
            from driveos.biometrics.preview import HUB
        except Exception:  # noqa: BLE001
            return lambda: None
        loop = asyncio.get_running_loop()
        seen: dict[str, str | None] = {"state": None}

        def refresh() -> None:
            co = self.co
            if co is None or co.method != "face" or co.stage != "verifying":
                return
            state = face_caption()["state"]
            if state == seen["state"]:
                return
            seen["state"] = state
            self.render()

        def on_state() -> None:
            loop.call_soon_threadsafe(refresh)

        HUB.add_state_listener(on_state)

        def stop() -> None:
            HUB.remove_state_listener(on_state)

        return stop

    async def _verify(self, co: Checkout, method: str) -> None:
        co.method, co.stage, co.detail = method, "verifying", ""
        self.render()
        stop_face = self._watch_face(method)
        try:
            verifier = self.bridge.verifier_for(method, self.host)
            if verifier is None:
                other = "Face ID" if method == "voice" else "Voice ID"
                await self._declined(co, f"{'Voice' if method == 'voice' else 'Face'} ID isn't available on this "
                                         f"device. Try {other}.", count=False)
                return
            co.verifier = verifier
            try:
                result = await verifier.verify(VERIFY_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("verification failed")
                result = None
                detail = f"Verification error: {type(e).__name__}"
            if co.closed:
                return
            if result is not None and result.success:
                await self._capture(co, result)
            else:
                await self._declined(co, (result.detail if result is not None else detail) or "Not verified")
        finally:
            stop_face()

    async def _declined(self, co: Checkout, detail: str, *, count: bool = True) -> None:
        if co.closed:
            return
        if count:
            co.attempts += 1
        co.stage, co.detail, co.otp, co.phone_note = "review", detail, None, ""
        if co.attempts >= MAX_ATTEMPTS:
            await self._close(co, announce=False)
            await self.say("Too many failed attempts, so I cancelled the checkout.")
            return
        self.render()
        await self.say(f"{detail.rstrip('.')}. You can try again, switch method, or cancel.")

    async def _capture(self, co: Checkout, result) -> None:
        card = self._active_card()
        place = co.cart.get("place") or {}
        minor = round(co.total * 100)
        tier = str(getattr(result.tier, "value", result.tier))
        snapshot = {"lines": [{"name": ln.get("name"), "qty": ln.get("qty"), "unit_price": ln.get("unit_price")}
                              for ln in co.cart.get("lines", [])],
                    "card": {"brand": card.brand, "last4": card.last4}, "place": place.get("name")}
        info = {"method": co.method, "auth": result.method, "tier": tier, "rule": co.risk.get("rule", ""),
                "total_minor": minor, "currency": co.currency, "card": f"{card.brand} ..{card.last4}"}
        try:
            self.ledger.upsert_transaction(co.id, state="captured", occupant_id=OCCUPANT, payer_id=OCCUPANT,
                                           merchant_id=str(place.get("id") or "unknown"), provider="nova-cabin",
                                           total_minor=minor, currency=co.currency, tier=tier, snapshot=snapshot)
            self.ledger.append(co.id, "authorised", info)
            self.ledger.append(co.id, "captured", info)
        except Exception:
            log.exception("ledger write failed")
        co.stage = "done"
        verified = "Voice ID"
        hero = None
        if co.method == "face":
            cap = face_caption()
            if cap["state"] != "verified":
                cap = {"state": "verified", "line": "Face verified"}
            verified = cap["line"]
            hero = {"kind": "face", "source": "image://facepreview/frame",
                    "label": cap["line"], "state": "verified"}
        receipt = {"kind": "receipt", "title": f"Paid · {co.place_name}",
                   "subtitle": f"{money(co.total, co.currency)} · {card.label}",
                   "hero": hero,
                   "rows": [{"icon": "flag", "label": "Total", "value": money(co.total, co.currency),
                             "tint": "#8EF0C4"},
                            {"icon": "user", "label": "Verified", "value": verified},
                            {"icon": "clock", "label": "Receipt", "value": co.id}],
                   "actions": []}
        self.engine.last_card = receipt
        self.engine.emit("card", receipt)
        self.engine.emit("checkout", {**self.snapshot(), "stage": "done"})
        self._release_cart()
        self.co = None
        co.closed = True
        self._set_system_line(None)
        self._drive = self._drive_target(place)
        self._drive_at = time.monotonic()
        if self._drive:
            self.engine.emit("ask", {"text": "Drive there?", "kind": "drive_there", "place": self._drive.get("name")})
        await self.say(f"Paid {money_words(co.total, co.currency)} with your {card.spoken}. Your order is placed.")

    def _release_cart(self) -> None:
        shop = getattr(self.engine, "shop", None)
        for name in ("checkout_done", "clear_cart", "clear"):
            fn = getattr(shop, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    log.exception("cart release failed")
                return

    async def _close(self, co: Checkout, *, announce: bool = True) -> None:
        co.closed = True
        if co.waiter is not None and not co.waiter.done():
            co.waiter.set_result(False)
        if co.verifier is not None and co.stage in ("verifying", "otp"):
            try:
                co.verifier.cancel()
            except Exception:
                log.exception("verifier cancel failed")
        if co.task is not None and not co.task.done() and co.task is not asyncio.current_task():
            co.task.cancel()
        if self.co is co:
            self.co = None
        self._set_system_line(None)
        cancelled = {"kind": "checkout", "title": "Checkout cancelled", "subtitle": co.place_name,
                     "rows": [], "actions": []}
        self.engine.last_card = cancelled
        self.engine.emit("card", cancelled)
        self.engine.emit("checkout", {"stage": "closed"})
        if announce:
            await self.say("Okay, I cancelled the checkout. Nothing was charged.")

    # one-time code -------------------------------------------------------------
    async def otp_roundtrip(self, timeout_s: float) -> bool:
        co = self.co
        if co is None:
            return False
        purpose = f"{money_words(co.total, co.currency)} at {co.place_name}"
        co.otp = await self.otp.issue(PHONE, purpose)
        waiter: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        co.waiter, co.stage, co.detail = waiter, "otp", ""
        self.render()
        await self.say(f"For this one I need a code. I sent it to your phone ending {PHONE[-4:]}. "
                       "Open the bell at the top right, then say the code one digit at a time, or type it.")
        try:
            return bool(await asyncio.wait_for(waiter, timeout_s))
        except TimeoutError:
            return False
        finally:
            co.waiter = None
            co.phone_note = ""
            self.engine.emit("notice", {})
            if not co.closed:
                co.stage = "verifying"

    async def submit_code(self, digits: str) -> None:
        co = self.co
        if co is None or co.otp is None or co.waiter is None or co.waiter.done():
            await self.say("I'm not waiting for a code right now.")
            return
        if len(digits) < OTP_LENGTH:
            await self.say(f"I heard {len(digits)} digits. I need {OTP_LENGTH}. Please say the whole code.")
            return
        res = self.otp.verify(co.otp.challenge_id, digits)
        if res.success:
            co.waiter.set_result(True)
            return
        if res.detail in ("locked", "expired", "unknown-challenge"):
            co.waiter.set_result(False)
            return
        self.render()
        await self.say(f"That code didn't match. You have {co.otp.attempts_left} tries left.")

    # card actions --------------------------------------------------------------
    async def _act_voice(self, payload: dict) -> None:
        self.start_approval("voice")

    async def _act_face(self, payload: dict) -> None:
        self.start_approval("face")

    async def _act_swap(self, payload: dict) -> None:
        await self.swap_card(None)

    async def _act_cancel(self, payload: dict) -> None:
        if self.co is not None:
            await self._close(self.co)

    async def swap_card(self, brand: str | None) -> None:
        co = self.co
        if co is None:
            return
        if co.stage in ("verifying", "otp"):
            await self.say("I can't change the card while I'm verifying. Cancel first.")
            return
        current = self.wallet.get(co.card_token)
        generic = brand is None or any(w in brand for w in ("other", "another", "different"))
        if not generic:
            target = self.wallet.by_brand(str(brand))
            if target is None:
                await self.say(f"I don't have a {normalise_brand(brand)} saved.")
                return
        else:
            target = self.wallet.other(co.card_token)
            if target is None or (current and target.token == current.token):
                await self.say("That's the only card in your wallet.")
                return
        co.card_token = target.token
        co.detail = ""
        self.render()
        await self.say(f"Switched to your {target.spoken}. Say approve when you're ready.")

    # intercept -------------------------------------------------------------------
    def _drive_target(self, place: dict) -> dict | None:
        target = dict(place or {})
        shop = getattr(self.engine, "shop", None)
        for src in (getattr(shop, "cart_place", None), getattr(shop, "place", None)):
            if src is None:
                continue
            for key in ("id", "name", "lat", "lon", "address"):
                if target.get(key) in (None, "") and getattr(src, key, None) not in (None, ""):
                    target[key] = getattr(src, key)
        if target.get("lat") is None or target.get("lon") is None:
            return None
        return target

    async def _drive_there(self) -> None:
        place = self._drive
        self._drive = None
        if not place:
            return
        nav = getattr(self.engine, "nav", None)
        agent = getattr(nav, "agent", None) if nav is not None else None
        if agent is None:
            await self.say("I can't start navigation right now.")
            return
        dest = type("Dest", (), {})()
        dest.id = str(place.get("id") or "")
        dest.name = str(place.get("name") or "destination")
        dest.lat = float(place["lat"])
        dest.lon = float(place["lon"])
        dest.address = str(place.get("address") or "")
        dest.category = ""
        try:
            await agent.start(dest)
        except Exception as exc:                                     # noqa: BLE001
            log.warning("drive there: %s", type(exc).__name__)
            await self.say("I couldn't start navigation.")

    async def intercept(self, text: str) -> bool:
        t = normalise(text)
        if self._drive and time.monotonic() - self._drive_at > DRIVE_ASK_TTL_S:
            self._drive = None
        if self.co is None and self._drive:
            if DRIVE_YES_RE.match(t):
                await self._drive_there()
                return True
            self._drive = None
            return bool(DRIVE_NO_RE.match(t))
        co = self.co
        if co is None:
            return False
        if not t:
            return False
        waiting = co.stage == "otp" and co.waiter is not None and not co.waiter.done()
        digits = spoken_digits(t, lenient=waiting)
        if digits is not None and (4 <= len(digits) <= 6 or co.stage == "otp"):
            await self.submit_code(digits)
            return True
        if waiting and not CANCEL_RE.match(t):
            await self.say(f"I'm waiting for the {OTP_LENGTH}-digit code. Say it one digit at a time, "
                           "or type it.")
            return True
        if CANCEL_RE.match(t):
            await self._close(co)
            return True
        if co.stage in ("verifying", "otp"):
            if APPROVE_RE.match(t) or FACE_RE.match(t):
                await self.say("I'm already checking. One moment.")
                return True
            return False
        if FACE_RE.match(t):
            self.start_approval("face")
            return True
        if APPROVE_RE.match(t):
            self.start_approval("voice")
            return True
        m = SWAP_RE.search(t)
        if m:
            await self.swap_card(m.group(1) if m.group(1) else None)
            return True
        return False


def install(engine) -> None:
    Payment(engine).install()
