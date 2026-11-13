"""Conversation + vehicle state the QML binds to.

Two drivers can feed it: the live Runtime (real voice, LLM, tools) or the
simulation providers (--sim / --demo). QML never knows which.

    state        idle | listening | thinking | speaking
    level        0..1 audio envelope (mic while listening, voice while speaking)
    userText     driver transcript (userPartial while still listening)
    replyText    assistant reply, streams as it is generated
    card         dict: kind, title, hero{kind,source,label|temp,cond}, rows[], actions[], sources[]
    nav          dict when navigating: name, map, distance, duration, eta, arrival
    vehicle      dict: battery_pct, range_mi, cabin_temp_f, seat_heat, …
    providers    dict: llm, stt, tts, maps, search, mic, voice …
"""
from __future__ import annotations

from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication

STATES = ("idle", "listening", "thinking", "speaking")


class AssistantController(QObject):
    stateChanged = Signal()
    levelChanged = Signal()
    userTextChanged = Signal()
    replyTextChanged = Signal()
    cardChanged = Signal()
    captionChanged = Signal()
    visemeChanged = Signal()
    navChanged = Signal()
    driverChanged = Signal()
    mapLiveChanged = Signal()
    mapOpenChanged = Signal()
    vehicleChanged = Signal()
    providersChanged = Signal()
    outsideChanged = Signal()
    keyboardChanged = Signal()
    tourChanged = Signal()
    noticeChanged = Signal()
    toast = Signal(str)
    actionPressed = Signal(str)          # visual press feedback (autopilot)

    # intents for whichever driver is attached
    micPressed = Signal()
    textSubmitted = Signal(str)
    actionTriggered = Signal(str, "QVariantMap")
    cabinTempNudged = Signal(float)
    seatHeatCycled = Signal()
    locationMapRequested = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._state = "idle"
        self._level = 0.0
        self._user = ""
        self._partial = False
        self._reply = ""
        self._card: dict = {}
        self._caption = ""
        self._vjaw = self._vround = 0.0
        self._vlive = False
        self._nav: dict = {}
        self._map_live = False
        self._map_open = False
        self._held_map = ""
        self._driver: dict = {"open": False, "tab": "voice", "state": {}}
        self._vehicle: dict = {"cabin_temp_f": 72, "battery_pct": 82, "range_mi": 254, "seat_heat": 0}
        self._providers: dict = {}
        self._outside = ""
        self._keyboard = False
        self._tour: dict = {}
        self._notice: dict = {}
        self.lips = None                       # LipSyncDriver, attached by main

    # ── helpers ────────────────────────────────────────────────────
    def _set(self, attr: str, value, signal) -> None:
        if getattr(self, attr) != value:
            setattr(self, attr, value)
            signal.emit()

    def apply(self, key: str, value) -> None:
        """Single entry point for engine updates (queued onto the GUI thread)."""
        if key == "state":
            self.state = value
        elif key == "level":
            self.level = value
        elif key == "caption":
            self.caption = value
        elif key == "user":
            self._partial = bool(value.get("partial"))
            self._user = value.get("text", "")
            self.userTextChanged.emit()
        elif key == "reply":
            self.replyText = value
        elif key == "card":
            self.card = value or {}
        elif key == "nav":
            self._set("_nav", value or {}, self.navChanged)
        elif key == "map_live":
            self._set("_map_live", bool(value), self.mapLiveChanged)
        elif key == "map_open":
            self._set("_map_open", bool(value), self.mapOpenChanged)
        elif key == "driver":
            self._set("_driver", dict(value or {}), self.driverChanged)
        elif key == "vehicle":
            self._set("_vehicle", value, self.vehicleChanged)
        elif key == "providers":
            self._set("_providers", value, self.providersChanged)
        elif key == "autopilot":
            self._set("_tour", value or {}, self.tourChanged)
        elif key == "press":
            self.actionPressed.emit(value)
            self.triggerAction(value)
        elif key == "outside":
            self._set("_outside", value, self.outsideChanged)
        elif key == "notice":
            self._set("_notice", dict(value or {}), self.noticeChanged)
        elif key == "toast":
            self.toast.emit(str(value))
        elif key == "keyboard":
            self.keyboardOpen = bool(value)
        elif key == "words" and self.lips:
            for w, start, dur in value:
                self.lips.add_word(w, start, dur)
        elif key == "lips_stop" and self.lips:
            self.lips.stop()
        elif key == "open_file":
            QDesktopServices.openUrl(QUrl(value))

    # ── properties ─────────────────────────────────────────────────
    def _g_state(self): return self._state
    def _s_state(self, v):
        if v not in STATES:
            raise ValueError(v)
        self._set("_state", v, self.stateChanged)
    state = Property(str, _g_state, _s_state, notify=stateChanged)

    def _g_level(self): return self._level
    def _s_level(self, v): self._set("_level", max(0.0, min(1.0, float(v))), self.levelChanged)
    level = Property(float, _g_level, _s_level, notify=levelChanged)

    def _g_user(self): return self._user
    def _s_user(self, v): self._set("_user", v, self.userTextChanged)
    userText = Property(str, _g_user, _s_user, notify=userTextChanged)

    def _g_partial(self): return self._partial
    userPartial = Property(bool, _g_partial, notify=userTextChanged)

    def _g_reply(self): return self._reply
    def _s_reply(self, v): self._set("_reply", v, self.replyTextChanged)
    replyText = Property(str, _g_reply, _s_reply, notify=replyTextChanged)

    def _g_card(self): return self._card
    def _s_card(self, v): self._set("_card", v or {}, self.cardChanged)
    card = Property("QVariantMap", _g_card, _s_card, notify=cardChanged)

    def _g_cardv(self): return bool(self._card)
    cardVisible = Property(bool, _g_cardv, notify=cardChanged)

    def _g_caption(self): return self._caption
    def _s_caption(self, v): self._set("_caption", v, self.captionChanged)
    caption = Property(str, _g_caption, _s_caption, notify=captionChanged)

    def _g_nav(self): return self._nav
    nav = Property("QVariantMap", _g_nav, notify=navChanged)

    def _g_map_live(self): return self._map_live
    mapLive = Property(bool, _g_map_live, notify=mapLiveChanged)

    def _g_map_open(self): return self._map_open
    mapOpen = Property(bool, _g_map_open, notify=mapOpenChanged)

    def _g_driver(self): return self._driver
    driver = Property("QVariantMap", _g_driver, notify=driverChanged)

    def _g_driver_open(self): return bool(self._driver.get("open"))
    driverOpen = Property(bool, _g_driver_open, notify=driverChanged)

    def _g_vehicle(self): return self._vehicle
    vehicle = Property("QVariantMap", _g_vehicle, notify=vehicleChanged)

    def _g_prov(self): return self._providers
    providers = Property("QVariantMap", _g_prov, notify=providersChanged)

    def _g_outside(self): return self._outside
    outside = Property(str, _g_outside, notify=outsideChanged)

    def _g_tour(self): return self._tour
    tour = Property("QVariantMap", _g_tour, notify=tourChanged)

    def _g_notice(self): return self._notice
    notice = Property("QVariantMap", _g_notice, notify=noticeChanged)

    def _g_notice_code(self): return str(self._notice.get("code") or "")
    noticeCode = Property(str, _g_notice_code, notify=noticeChanged)

    def _g_sim(self): return bool(self._providers.get("sim"))
    simulation = Property(bool, _g_sim, notify=providersChanged)

    def _g_kb(self): return self._keyboard
    def _s_kb(self, v): self._set("_keyboard", bool(v), self.keyboardChanged)
    keyboardOpen = Property(bool, _g_kb, _s_kb, notify=keyboardChanged)

    # lip-sync targets (see cabin/lipsync.py)
    def set_viseme(self, jaw: float, rnd: float, live: bool) -> None:
        if (jaw, rnd, live) != (self._vjaw, self._vround, self._vlive):
            self._vjaw, self._vround, self._vlive = jaw, rnd, live
            self.visemeChanged.emit()

    def _g_vjaw(self): return self._vjaw
    visemeJaw = Property(float, _g_vjaw, notify=visemeChanged)

    def _g_vround(self): return self._vround
    visemeRound = Property(float, _g_vround, notify=visemeChanged)

    def _g_vlive(self): return self._vlive
    visemesLive = Property(bool, _g_vlive, notify=visemeChanged)

    # ── slots called from QML ──────────────────────────────────────
    @Slot()
    def toggleListening(self) -> None:
        self.micPressed.emit()

    @Slot(str)
    def setState(self, s: str) -> None:
        self.state = s

    @Slot(str)
    def submitText(self, text: str) -> None:
        self.keyboardOpen = False
        if text.strip():
            self.textSubmitted.emit(text.strip())

    @Slot(str)
    def triggerAction(self, key: str) -> None:
        payload = {}
        for a in self._card.get("actions", []):
            if a.get("key") == key:
                payload = a.get("payload") or {}
        if key == "share":
            QGuiApplication.clipboard().setText(payload.get("text", ""))
            self.toast.emit("Copied — paste to share")
        elif key == "open":
            if self._providers.get("sim"):
                self.toast.emit(f"Would open {payload.get('url', '')} on your phone")
            else:
                QDesktopServices.openUrl(QUrl(payload.get("url", "")))
        elif key == "call":
            self.toast.emit(f"Calling {payload.get('phone', '')} via connected phone")
        else:
            self.actionTriggered.emit(key, payload)

    @Slot()
    def openLocationMap(self) -> None:
        self._set("_map_open", True, self.mapOpenChanged)
        if not self._nav.get("name"):
            self._set("_nav", {"name": "Current location", "eta": "—", "duration": "—",
                               "distance": "—", "arrival": "—"}, self.navChanged)
        elif self._held_map and "map" not in self._nav:
            nav = dict(self._nav)
            nav["map"] = self._held_map
            self._set("_nav", nav, self.navChanged)
        self.locationMapRequested.emit(True)

    @Slot()
    def closeLocationMap(self) -> None:
        self._set("_map_open", False, self.mapOpenChanged)
        if self._nav.get("map") is not None:
            self._held_map = self._nav.get("map")
            nav = dict(self._nav)
            nav.pop("map", None)
            self._set("_nav", nav, self.navChanged)
        self.locationMapRequested.emit(False)

    @Slot()
    def closeNav(self) -> None:
        self._set("_nav", {}, self.navChanged)
        self.actionTriggered.emit("nav_end", {})
        self.closeLocationMap()

    @Slot(float)
    def nudgeCabinTemp(self, delta: float) -> None:
        self.cabinTempNudged.emit(delta)

    @Slot()
    def cycleSeatHeat(self) -> None:
        self.seatHeatCycled.emit()
