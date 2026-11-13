"""Driver sheet controller. QML sends driver_* actions; this emits the driver map."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from driveos.biometrics.enroll import ConsentRequired
from driveos.server.enroll import EnrollError

from ..wallet import Wallet
from .consent import ConsentStore
from .face_enroll import FaceEnroll
from .memory_tab import MemoryTab
from .paths import biometrics_home
from .voice_enroll import VoiceEnroll

log = logging.getLogger("driver.sheet")

TABS = ("voice", "face", "cards", "memory")
EXPECTED = (ConsentRequired, EnrollError, ValueError)


class DriverSheet:
    def __init__(self, engine, *, wallet: Wallet | None = None, voice=None, face=None, memory=None,
                 home: Path | None = None):
        self.engine = engine
        self.open = False
        self.tab = "voice"
        self.notice = ""
        self.pending: dict | None = None
        self.home = Path(home) if home is not None else biometrics_home(engine)
        self.consent = ConsentStore(self.home)
        self.wallet = wallet or _wallet(engine)
        self.voice = voice or VoiceEnroll(engine, home=self.home, consent=self.consent, on_change=self._changed,
                                          on_enrolled=self._enrolled)
        self.face = face or FaceEnroll(engine, home=self.home, consent=self.consent, on_change=self._changed,
                                       voice_active=lambda: self.voice.state().get("busy", False))
        self.memory = memory or MemoryTab(engine)
        engine.action_handlers.update({
            "driver_open": self._open, "driver_close": self._close, "driver_tab": self._tab,
            "driver_consent": self._consent, "driver_confirm": self._confirm, "driver_cancel": self._cancel,
            "driver_voice_start": self._voice_start, "driver_voice_cancel": self._voice_cancel, "driver_voice_take": self._voice_take,
            "driver_voice_rename": self._voice_rename, "driver_voice_delete": self._voice_delete,
            "driver_face_start": self._face_start, "driver_face_select": self._face_select,
            "driver_face_rename": self._face_rename, "driver_face_delete": self._face_delete,
            "driver_face_dismiss": self._face_dismiss,
            "driver_card_add": self._card_add, "driver_card_edit": self._card_edit,
            "driver_card_remove": self._card_remove, "driver_card_default": self._card_default,
            "driver_memory_forget": self._forget, "driver_memory_forget_all": self._forget_all,
        })

    # state -------------------------------------------------------------------
    def snapshot(self) -> dict:
        face = self.face.state()
        pending = self.pending
        if pending is None and face.get("link"):
            pending = {"kind": "face_link", "text": face.get("message") or "Link this face?"}
        return {"open": self.open, "tab": self.tab, "state": {
            "voice": self.voice.state(), "face": face,
            "cards": self.wallet.public() if self.wallet is not None else [],
            "memory": self.memory.snapshot(), "consent": self.consent.all(), "pending": pending,
            "notice": self.notice}}

    def _publish(self) -> None:
        self.engine.emit("driver", self.snapshot())

    def _changed(self) -> None:
        if self.open:
            self._publish()

    def _enrolled(self) -> None:
        bridge = getattr(getattr(self.engine, "payment", None), "bridge", None)
        reset = getattr(bridge, "reset", None)
        if callable(reset):
            reset()

    async def _refresh(self) -> None:
        for part in (self.voice, self.face, self.memory):
            try:
                await part.refresh()
            except Exception:
                log.exception("driver sheet refresh failed")

    async def _guard(self, coro) -> None:
        """Run an action; a refused or invalid request becomes the sheet's notice instead of a crash."""
        try:
            await coro
        except EXPECTED as e:
            self.notice = str(e) or type(e).__name__
        self._publish()

    # open / tabs -------------------------------------------------------------
    async def _open(self, _payload: dict) -> None:
        self.open = True
        self.notice = ""
        self._publish()
        await self._refresh()
        self._publish()

    async def _stop_voice_take(self) -> None:
        """Closing the sheet or leaving the voice tab must release the mic."""
        try:
            stage = str((self.voice.state() or {}).get("stage") or "")
        except Exception:
            log.exception("voice state unreadable")
            return
        if stage not in ("recording", "analyzing"):
            return
        try:
            await self.voice.cancel()
        except Exception:
            log.exception("could not cancel the voice take")

    async def _close(self, _payload: dict) -> None:
        await self._stop_voice_take()
        self.open = False
        self.pending = None
        self._publish()

    async def _tab(self, payload: dict) -> None:
        await self._stop_voice_take()
        tab = str((payload or {}).get("tab") or "voice")
        if tab in TABS:
            self.tab = tab
        self.open = True
        self.notice = ""
        self.pending = None
        self._publish()
        await self._refresh()
        self._publish()

    # consent and confirmation ------------------------------------------------
    async def _consent(self, payload: dict) -> None:
        kind = str((payload or {}).get("kind") or "")
        value = (payload or {}).get("value") is True

        async def go():
            self.consent.set(kind, value)
            self.notice = ""
        await self._guard(go())

    def _ask(self, kind: str, text: str, **args) -> None:
        self.pending = {"kind": kind, "text": text, **args}
        self.notice = ""

    async def _confirm(self, _payload: dict) -> None:
        pending, self.pending = self.pending, None
        if pending is None:
            kind = "face_link" if self.face.state().get("link") else ""
            pending = {"kind": kind} if kind else None
        if pending is None:
            self._publish()
            return

        async def go():
            kind = pending["kind"]
            if kind == "voice_delete":
                await self.voice.delete()
            elif kind == "face_delete":
                await self.face.delete(pending["profile_id"])
            elif kind == "face_link":
                await self.face.link()
            elif kind == "card_remove":
                self.wallet.remove(pending["token"])
            elif kind == "memory_forget_all":
                await self.memory.forget_all()
        await self._guard(go())

    async def _cancel(self, _payload: dict) -> None:
        if self.pending is not None:
            self.pending = None
        elif self.face.state().get("link"):
            self.face.dismiss()
        self._publish()

    # voice -------------------------------------------------------------------
    async def _voice_start(self, payload: dict) -> None:
        await self._guard(self.voice.start(str((payload or {}).get("name") or "Driver")))

    async def _voice_cancel(self, _payload: dict) -> None:
        await self.voice.cancel()
        self._publish()

    async def _voice_take(self, _payload: dict) -> None:
        async def go():
            self.voice.take()
        await self._guard(go())

    async def _voice_rename(self, payload: dict) -> None:
        await self._guard(self.voice.rename(str((payload or {}).get("name") or "")))

    async def _voice_delete(self, _payload: dict) -> None:
        self._ask("voice_delete", "Delete your Voice ID? Voice payments will fall back to the other checks.")
        self._publish()

    # face --------------------------------------------------------------------
    async def _face_start(self, payload: dict) -> None:
        payload = payload or {}
        await self._guard(self.face.start(str(payload.get("name") or ""), payload.get("profile_id") or "new"))

    async def _face_select(self, payload: dict) -> None:
        await self._guard(self.face.select(str((payload or {}).get("profile_id") or "")))

    async def _face_rename(self, payload: dict) -> None:
        payload = payload or {}
        await self._guard(self.face.rename(str(payload.get("profile_id") or ""), str(payload.get("name") or "")))

    async def _face_delete(self, payload: dict) -> None:
        pid = str((payload or {}).get("profile_id") or "")
        name = next((p["name"] for p in self.face.state().get("profiles", []) if p.get("id") == pid), "")
        self._ask("face_delete", f"Delete the face profile {name}?".replace(" ?", "?") if name else
                  "Delete this face profile?", profile_id=pid)
        self._publish()

    async def _face_dismiss(self, _payload: dict) -> None:
        self.face.dismiss()
        self._publish()

    # cards -------------------------------------------------------------------
    async def _card_add(self, payload: dict) -> None:
        payload = payload or {}

        async def go():
            last4 = str(payload.get("last4") or "").strip()
            if not re.fullmatch(r"\d{4}", last4):
                raise ValueError("Enter only the last four digits of the card.")
            expiry = str(payload.get("expiry") or "").strip()
            if not re.fullmatch(r"\d{2}/\d{2}", expiry):
                raise ValueError("Expiry must look like MM/YY.")
            brand = str(payload.get("brand") or "Visa").strip() or "Visa"
            card = self.wallet.add(brand, last4, expiry, default=bool(payload.get("default")))
            label = str(payload.get("label") or "").strip()
            if label:
                self.wallet.edit(card.token, label=label)
            self.notice = ""
        await self._guard(go())

    async def _card_edit(self, payload: dict) -> None:
        payload = payload or {}

        async def go():
            self.wallet.edit(str(payload.get("token") or ""), label=payload.get("label"),
                             expiry=payload.get("expiry"))
            self.notice = ""
        await self._guard(go())

    async def _card_remove(self, payload: dict) -> None:
        token = str((payload or {}).get("token") or "")
        card = self.wallet.get(token) if self.wallet is not None else None
        self._ask("card_remove", f"Remove {card.label}?" if card else "Remove this card?", token=token)
        self._publish()

    async def _card_default(self, payload: dict) -> None:
        async def go():
            self.wallet.set_default(str((payload or {}).get("token") or ""))
        await self._guard(go())

    # memory ------------------------------------------------------------------
    async def _forget(self, payload: dict) -> None:
        await self._guard(self.memory.forget(str((payload or {}).get("field") or "")))

    async def _forget_all(self, _payload: dict) -> None:
        self._ask("memory_forget_all", "Forget everything Nova remembers about you, including past conversations?")
        self._publish()


def _wallet(engine) -> Wallet | None:
    payment = getattr(engine, "payment", None)
    return getattr(payment, "wallet", None)
