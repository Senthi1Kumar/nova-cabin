"""Conversation engine — runs on its own asyncio loop thread.

          ┌─────── speech onset (handsfree) / tap ───────┐
          ▼                                              │
  idle ─► listening ─► (endpoint) ─► thinking ─► speaking ─► idle
             ▲                          │            │
             └──── barge-in / tap ──────┴────────────┘

* Mic frames come from PortAudio, are VAD-gated, and stream to STT.
* The LLM streams text; complete sentences are synthesised while later ones
  are still generating, so speech starts after the first sentence.
* Tool calls run concurrently; each produces a card pushed to the UI.
* Talking over the assistant (or tapping the orb) cancels LLM, TTS and
  playback within one audio frame and starts listening.

UI updates leave through `emit(key, value)` (a Qt signal, so thread-safe).
"""
from __future__ import annotations

import asyncio
import collections
import datetime as dt
import json
import logging
import re
import time
from pathlib import Path
from typing import Awaitable, Callable

import httpx
import numpy as np
from driveos.config import LocationConfig

from . import frontend
from .audio import FRAME_MS, VAD, Microphone, Player, VADConfig, resolve_device, to_pcm16
from .brain.compound import COMPOUND_HINT, outcome_from, merge_reply
from .brain.freshness import label_stale, stamp
from .brain.guarantee import (ASK_PLACE, LET_ME_TRY, NUDGE, cant_first, needs_tool, rules_call,
                              tool_names)
from .config import Settings
from .llm import ChatClient
from .providers.wake import WakeMatcher, split_phrases
from .services.info import Charging, Search, Weather
from .services.maps import Maps
from .stt import make_stt
from .tools import Toolbox, Vehicle, gather_tools, tool_result_json
from .tts import Speech, estimate_words, make_tts

log = logging.getLogger("engine")

SENT_BOUNDARY = re.compile(r"[.!?…][\"”’)]*\s")


def frame_level(frame: np.ndarray) -> float:
    """0..1 envelope of one mic frame, so the orb moves while a tap is open."""
    if frame.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(np.square(np.asarray(frame, dtype=np.float32)))))
    return max(0.0, min(1.0, rms * 4.0))


def clean_for_speech(s: str) -> str:
    s = re.sub(r"[*_`#>|]", "", s)
    s = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", s)
    s = re.sub(r"https?://\S+", "", s)
    return re.sub(r"\s+", " ", s).strip()


class Engine:
    MAX_ROUNDS = 5
    HISTORY = 18
    _spans = None
    _tel_failed = False

    def __init__(self, cfg: Settings, emit: Callable[[str, object], None], cache: Path,
                 sim: bool = False, autopilot: bool = False) -> None:
        self.cfg = cfg
        self._emit_raw = emit
        self.emit = self._emit_traced
        self._spans = None
        self._tel_failed = False
        self.cache = cache
        self.sim = sim
        self.autopilot_enabled = autopilot
        self.autopilot = None
        self.last_card: dict | None = None
        self.nav_open = False
        self.loop: asyncio.AbstractEventLoop | None = None
        self.state = "idle"
        self.messages: list[dict] = []
        self._turn: asyncio.Task | None = None
        self._speaker: asyncio.Task | None = None
        self._speak_q: asyncio.Queue | None = None
        self._spoken = ""                      # text actually voiced this turn
        self._cant, self._cant_call, self._vet_first, self._turn_outcomes = False, None, False, []
        self._listen_session = None
        self._listen_started = 0.0
        self._listen_armed_at = 0.0
        self._heard_speech = False
        self._preroll: collections.deque = collections.deque()
        self._clean_active = False             # STT/pre-roll are fed the echo-cancelled stream
        self._clean_anchor = None              # last clean frame already consumed
        self._clean_raw_n = self._clean_n = 0  # raw / clean frames seen since the stream became active
        self._clean_failed = False
        self._clean_fe = None                  # front end the clean-stream state belongs to
        self._preroll_t: collections.deque = collections.deque()   # (capture time, pcm) for late starts
        self.PREROLL_KEEP_S = cfg.num("WAKE_WINDOW_S", 1.2) + 2.0
        # extension points card-action handlers keyed by action key,
        # ``async fn(payload) -> None``; turn intercepts ``async fn(text) -> bool`` run before the LLM
        # (True = handled, e.g. a spoken one-time code or an approval phrase that must never reach a model);
        # extra system-prompt lines appended by plugins.
        self.action_handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}
        self.intercepts: list[Callable[[str], Awaitable[bool]]] = []
        self.system_extra: list[str] = []
        self.planner = None
        self.memory = None
        # shutdown seam: plugins append ``async fn() -> None``; stop() awaits them in reverse order
        self.closers: list[Callable[[], Awaitable[None]]] = []
        self._level_tick = 0
        self.handsfree = cfg.get("LISTEN_MODE", "handsfree").lower() == "handsfree"
        self.barge_in = cfg.flag("BARGE_IN", True)
        self.wake = cfg.get("WAKE_WORD").lower().strip()
        # None: ask the front end when the transcript arrives (tests and typed turns).
        # A bool is the decision taken in _end_listen, while state is still "listening".
        self._wake_needed: bool | None = None
        self._from_tap = False
        self._playback_ended_at = 0.0
        self.vad = VAD(VADConfig(sensitivity=cfg.num("VAD_SENSITIVITY", 0.6),
                                 end_silence_ms=int(cfg.num("VAD_END_SILENCE_MS", 700))))
        if hasattr(self.vad, "stop"):
            async def _stop_vad() -> None:
                self.vad.stop()
            self.closers.append(_stop_vad)
        sim = LocationConfig()
        self.vehicle = Vehicle(
            name=cfg.get("VEHICLE_NAME", "Model Y"), lat=cfg.num("VEHICLE_LAT", sim.fallback.lat),
            lon=cfg.num("VEHICLE_LON", sim.fallback.lon),
            place=cfg.get("VEHICLE_PLACE", sim.fallback_label),
            battery_pct=cfg.num("VEHICLE_BATTERY_PCT", 82), range_full_mi=cfg.num("VEHICLE_RANGE_FULL_MI", 310),
            cabin_temp_f=cfg.num("VEHICLE_CABIN_TEMP_F", 72))

    # ── lifecycle ────────────────────────────────────────────────
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.http = httpx.AsyncClient(timeout=self.cfg.num("HTTP_TIMEOUT_S", 20), follow_redirects=True,
                                      limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
        if self.sim:
            from . import sim
            parts = sim.build(self)
            self.maps, self.search, self.weather = parts["maps"], parts["search"], parts["weather"]
            self.charging, self.llm, self.stt, self.tts = parts["charging"], parts["llm"], parts["stt"], parts["tts"]
        else:
            self.maps = Maps(self.cfg, self.http, self.cache)
            self.search = Search(self.cfg, self.http, self.cache)
            self.weather = Weather(self.cfg, self.http)
            self.charging = Charging(self.cfg, self.http, self.maps)
            conf = self.cfg.llm()
            self.llm = ChatClient(self.http, conf) if conf else None
            self.stt = make_stt(self.cfg, self.http)
            self.tts = make_tts(self.cfg, self.http)
        self.tools = Toolbox(self.cfg, self.maps, self.search, self.weather, self.charging, self.vehicle)
        from . import plugins
        plugins.load(self)
        if self.memory:
            try:
                await self.memory.load()
            except Exception:
                log.exception("memory unavailable; continuing without it")
                self.memory = None
        try:
            from .brain import Planner
            from .brain.freshness import PROMPT_LINE
            self.planner = Planner(self)
            self.system_extra.append(PROMPT_LINE)
        except Exception:                                            # noqa: BLE001
            log.exception("planner unavailable")
            self.planner = None

        self.player = Player(resolve_device(self.cfg.get("AUDIO_OUTPUT_DEVICE"), kind="output"))
        self.player.gain = max(0.5, min(6.0, self.cfg.num("PLAYBACK_GAIN", self.player.gain)))
        self.player.on_drained = lambda: self.loop.call_soon_threadsafe(self._on_drained)
        self.mic = Microphone(lambda f: self.loop.call_soon_threadsafe(self._on_frame, f),
                              resolve_device(self.cfg.get("AUDIO_INPUT_DEVICE")))
        self.voice_ok = self.mic.available and self.stt is not None

        self.emit("providers", {
            "llm": f"{self.llm.label} · {self.llm.model}" if self.llm else "No LLM key",
            "stt": self.stt.label if self.stt else "—", "tts": self.tts.label if self.tts else "—",
            "maps": self.maps.label, "search": self.search.label,
            "mic": self.mic.available, "speaker": self.player.available, "voice": self.voice_ok,
            "handsfree": self.handsfree, "sim": self.sim})
        self.emit("vehicle", self.vehicle.snapshot())
        if self.sim:
            from .sim import Autopilot
            self.autopilot = Autopilot(self)
            self.emit("toast", "Simulation mode — offline, no API keys in use")
            if self.autopilot_enabled:
                self.autopilot.start()
            else:
                self.emit("autopilot", {"running": False, "step": 0})
        elif not self.llm:
            self.emit("toast", "Add an LLM key to .env to talk to the assistant")
        elif not self.mic.available:
            self.emit("toast", "Microphone unavailable — use the keyboard")
        elif not self.stt:
            self.emit("toast", "Add a speech-to-text key (Deepgram, OpenAI or Groq) for voice")
        self._set_state("idle", "")
        asyncio.create_task(self._ambient())

    async def _ambient(self) -> None:
        """Status-bar outside temperature, refreshed every 15 minutes."""
        while True:
            try:
                wx = await self.weather.forecast(self.vehicle.lat, self.vehicle.lon, 1)
                self.emit("outside", f"{wx['now']['temp']}{wx['unit']}")
            except Exception as e:                                   # noqa: BLE001
                log.info("ambient weather unavailable: %s", e)
            await asyncio.sleep(900)

    async def stop(self) -> None:
        await self._cancel_turn()
        for closer in reversed(self.closers):
            try:
                await asyncio.wait_for(closer(), 3)
            except Exception:                                        # noqa: BLE001
                log.exception("closer failed")
        self.mic.close()
        self.player.close()
        await self.http.aclose()

    # ── state ────────────────────────────────────────────────────
    def _tel_call(self, name: str, *args, **kw):
        """Call a telemetry method; any failure is logged once and ignored."""
        try:
            tel = getattr(self, "telemetry", None)
            if tel is not None:
                return getattr(tel, name)(*args, **kw)
        except Exception:                                            # noqa: BLE001
            if not self._tel_failed:
                self._tel_failed = True
                log.exception("telemetry disabled for this run after an error")
        return None

    def _emit_traced(self, k: str, v: object) -> None:
        if not self._tel_failed:
            self._tel_call("event", k, v)
        self._emit_raw(k, v)

    def _span_begin(self, mark: str | None = "speech_end") -> None:
        self._spans = self._tel_call("turn") if not self._tel_failed else None
        if mark:
            self._mark(mark)

    def _mark(self, name: str, at: float | None = None, replace: bool = False) -> None:
        sp = self._spans
        if sp is None or self._tel_failed:
            return
        try:
            sp.mark(name, at=at, replace=replace)
        except Exception:                                            # noqa: BLE001
            if not self._tel_failed:
                self._tel_failed = True
                log.exception("telemetry disabled for this run after an error")

    async def _run_tools(self, calls: list[dict]):
        engine = self

        class _Timed:
            async def run(self, name, args):
                t = time.monotonic()
                try:
                    out = await engine.tools.run(name, args)
                except Exception as e:                               # noqa: BLE001
                    engine._tel_call("tool", name, args, None, time.monotonic() - t, error=str(e)[:200])
                    raise
                res = out[0] if isinstance(out, tuple) else out
                err = res.get("error") if isinstance(res, dict) else None
                engine._tel_call("tool", name, args, res, time.monotonic() - t,
                                 error=str(err)[:200] if err else None)
                return out
        out = await gather_tools(_Timed(), calls)
        self._mark("tools_done", replace=True)
        return out

    def _set_state(self, s: str, caption: str | None = None) -> None:
        self.state = s
        if caption is not None:
            self.emit("caption", caption)
        self.emit("state", s)
        if s == "idle":
            self.emit("level", 0.0)

    # ── audio frames (event-loop thread) ─────────────────────────
    def _on_frame(self, frame: np.ndarray) -> None:
        playing = self.player.playing
        ev, level = self.vad.update(frame, self.player.level_db if playing else None)
        chunks = self._mic_chunks(frame)
        for pcm in chunks:
            self._preroll.append(pcm)
        while len(self._preroll) > self.vad.cfg.preroll_ms // FRAME_MS:
            self._preroll.popleft()
        t_frame = getattr(frame, "t", 0.0) or time.monotonic()
        for pcm in chunks:
            self._preroll_t.append((t_frame, pcm))
        while self._preroll_t and self._preroll_t[0][0] < t_frame - self.PREROLL_KEEP_S:
            self._preroll_t.popleft()

        st = self.state
        if st == "listening":
            self._level_tick += 1
            if self._level_tick % 2 == 0:
                self.emit("level", frame_level(frame))
            if self._listen_session is not None:
                for pcm in chunks:
                    asyncio.ensure_future(self._listen_session.feed(pcm))
            if ev == "start":
                self._heard_speech = True
            if self._heard_speech and (ev == "end" or
                                       time.monotonic() - self._listen_started > self.vad.cfg.max_utterance_s):
                self._end_listen()
            elif not self._heard_speech and time.monotonic() - self._listen_armed_at > 7.0:
                self._abort_listen("")
        elif st == "idle":
            if ev == "start" and self.handsfree and self.voice_ok:
                self._user_activity("you spoke")
                self._begin_listen(heard=True, onset_t=getattr(self.vad, "last_onset_t", 0.0))
        elif st in ("thinking", "speaking"):
            if ev == "start" and self.barge_in and self.voice_ok:
                log.info("barge-in")
                self._user_activity("you spoke")
                onset = getattr(self.vad, "last_onset_t", 0.0)
                asyncio.ensure_future(self._barge_in(onset))

    CLEAN_MAX_LAG = 10
    ONSET_LEAD_S = 0.3

    def _mic_chunks(self, frame: np.ndarray) -> list[bytes]:
        """PCM for STT and the pre-roll: echo-cancelled frames, in order, while software AEC is on.

        The clean stream trails the raw one by a frame or two and is consumed past the last frame
        taken, so each is used once. It falls back to the raw frame if it stalls or overflows."""
        fe = getattr(self.vad, "_frontend", None)
        clean = getattr(fe, "clean_frames", None)
        if fe is not self._clean_fe:
            self._clean_fe = fe
            self._clean_failed = False
            self._clean_active = False
        if clean is None or self._clean_failed or fe.stats.get("aec") != "on":
            self._clean_active = False
            return [to_pcm16(frame)]
        if not self._clean_active:
            self._clean_active = True
            self._clean_anchor = clean[-1] if clean else None
            self._clean_raw_n = self._clean_n = 0
        self._clean_raw_n += 1
        fresh, found = [], self._clean_anchor is None
        for c in reversed(list(clean)):
            if c is self._clean_anchor:
                found = True
                break
            fresh.append(c)
        fresh.reverse()
        if fresh:
            self._clean_anchor = fresh[-1]
            self._clean_n += len(fresh)
        if not found or self._clean_raw_n - self._clean_n > self.CLEAN_MAX_LAG:
            self._clean_failed = True
            log.warning("echo-cancelled mic stream stalled; using raw mic audio")
            return [to_pcm16(frame)]
        return [to_pcm16(c) for c in fresh]

    def _on_drained(self) -> None:
        self._playback_ended_at = time.monotonic()

    # ── listening ────────────────────────────────────────────────
    def _begin_listen(self, heard: bool, onset_t: float = 0.0) -> None:
        self._clean_failed = False
        self._from_tap = not heard
        self._set_state("listening", "Listening")
        self.emit("user", {"text": "", "partial": True})
        self.emit("reply", "")
        self._heard_speech = heard
        self._listen_started = self._listen_armed_at = time.monotonic()

        async def partial(text: str) -> None:
            if self.state == "listening":
                self.emit("user", {"text": text, "partial": True})
        self._listen_session = self.stt.session(partial)
        if onset_t > 0.0:
            since = onset_t - self.ONSET_LEAD_S - 1e-6
            lead = [p for t, p in self._preroll_t if t >= since]
        else:
            lead = list(self._preroll)
        for p in lead:
            asyncio.ensure_future(self._listen_session.feed(p))

    def _end_listen(self) -> None:
        session, self._listen_session = self._listen_session, None
        # Read the exemption while still listening. _from_voice runs after the
        # state has already moved to thinking, which is too late for a check
        # that looks at the current state.
        if self._from_tap:
            self._wake_needed = False
        else:
            try:
                self._wake_needed = bool(frontend.wake_required())
            except Exception:                                    # noqa: BLE001
                self._wake_needed = True
        self._from_tap = False
        self._span_begin()
        self._set_state("thinking", "Thinking")
        self._turn = asyncio.create_task(self._from_voice(session))

    def _abort_listen(self, toast: str) -> None:
        self._wake_needed = None
        self._from_tap = False
        s, self._listen_session = self._listen_session, None
        if s:
            asyncio.ensure_future(s.cancel())
        self.emit("user", {"text": "", "partial": False})
        self._set_state("idle", "")
        if toast:
            self.emit("toast", toast)

    async def _from_voice(self, session) -> None:
        try:
            text = await session.finish()
            self._mark("stt_done")
        except Exception as e:                                       # noqa: BLE001
            log.warning("stt failed: %s", e)
            self.emit("toast", f"Speech recognition failed: {str(e)[:80]}")
            self._set_state("idle", "")
            return
        needed = self._wake_needed
        self._wake_needed = None
        if self.wake and needed is None:
            try:
                needed = bool(frontend.wake_required())
            except Exception:                                    # noqa: BLE001
                needed = True
        if self.wake and needed:
            ok, text = WakeMatcher(split_phrases(self.wake)).match(text)
            if not ok:
                self.emit("user", {"text": "", "partial": False})
                self._set_state("idle", "")
                return
        if len(re.sub(r"[^\w]", "", text)) < 2:
            self.emit("user", {"text": "", "partial": False})
            self._set_state("idle", "")
            return
        self.emit("user", {"text": text, "partial": False})
        await self._respond(text)

    async def _intercept(self, text: str) -> bool:
        for fn in self.intercepts:
            try:
                if await fn(text):
                    return True
            except Exception:                                        # noqa: BLE001
                log.exception("intercept failed")
        return False

    # ── public controls (thread-safe via call_soon_threadsafe) ───
    def _user_activity(self, why: str) -> None:
        if self.autopilot and not self.autopilot.paused and self.autopilot.task and not self.autopilot.task.done():
            self.autopilot.pause(why)

    def sim_next(self) -> None:
        if self.autopilot:
            self.autopilot.next()

    def sim_toggle(self) -> None:
        if self.autopilot:
            self.autopilot.toggle()

    def show_location_map(self) -> None:
        live = getattr(self, "live_map", None)
        if live is not None:
            live.show()
            return
        self.emit("map_open", True)
        self.emit("nav", {"name": "Current location", "eta": "—", "duration": "—",
                          "distance": "—", "arrival": "—"})

    def hide_location_map(self) -> None:
        live = getattr(self, "live_map", None)
        if live is not None:
            live.hide()
            return
        self.emit("map_open", False)

    def tap(self) -> None:
        self._user_activity("you tapped")
        st = self.state
        if st == "idle":
            if not self.voice_ok:
                self.emit("keyboard", True)
                return
            self._begin_listen(heard=False)
        elif st == "listening":
            if self._heard_speech:
                self._end_listen()
            else:
                self._abort_listen("")
        else:
            asyncio.ensure_future(self._interrupt_to_idle())

    def submit_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self._user_activity("you typed")

        async def go():
            await self._cancel_turn()
            if self._listen_session:
                self._abort_listen("")
            self._span_begin()
            self.emit("user", {"text": text, "partial": False})
            self._set_state("thinking", "Thinking")
            self._turn = asyncio.current_task()
            await self._respond(text)
        asyncio.ensure_future(go())

    def action(self, key: str, payload: dict) -> None:
        async def go():
            try:
                if key in self.action_handlers:
                    await self.action_handlers[key](dict(payload))
                elif key == "navigate":
                    self.emit("caption", "Routing")
                    nav = await self.tools.build_nav(payload["name"], payload["lat"], payload["lon"],
                                                     payload.get("address", ""))
                    self.emit("nav", nav)
                    self.nav_open = True
                    self.emit("caption", "")
                elif key == "calendar":
                    path = self._write_ics(payload)
                    self.emit("toast", f"Added to calendar · {path.name}")
                    if not self.sim:
                        self.emit("open_file", path.as_uri())
            except Exception as e:                                   # noqa: BLE001
                log.exception("action failed")
                self.emit("caption", "")
                self.emit("toast", f"Couldn’t complete: {str(e)[:80]}")
        asyncio.ensure_future(go())

    def set_cabin_temp(self, delta: float) -> None:
        self.vehicle.cabin_temp_f = max(60, min(85, self.vehicle.cabin_temp_f + delta))
        self.emit("vehicle", self.vehicle.snapshot())

    def set_seat_heat(self) -> None:
        self.vehicle.seat_heat = (self.vehicle.seat_heat + 1) % 4
        self.emit("vehicle", self.vehicle.snapshot())

    # ── interruption ─────────────────────────────────────────────
    async def _cancel_turn(self) -> None:
        self.player.flush()
        self.emit("lips_stop", None)
        for t in (self._speaker, self._turn):
            if t and not t.done() and t is not asyncio.current_task():
                t.cancel()
        for t in (self._speaker, self._turn):
            if t and not t.done() and t is not asyncio.current_task():
                try:
                    await t
                except (asyncio.CancelledError, Exception):          # noqa: BLE001
                    pass
        self._speaker = self._turn = None

    async def simulate_voice(self, text: str) -> None:
        """Play a request as if the driver spoke it: listening UI, live partials, mic level,
        endpoint pause — then the normal turn. Talking over a reply = barge-in."""
        if self.state in ("thinking", "speaking"):
            log.info("simulated barge-in")
            await self._cancel_turn()
        if self._listen_session:
            s, self._listen_session = self._listen_session, None
            await s.cancel()
        self._set_state("listening", "Listening")
        self.emit("user", {"text": "", "partial": True})
        self.emit("reply", "")
        rng = np.random.default_rng(abs(hash(text)) % 2**32)

        async def level_for(secs: float, voiced: bool) -> None:
            t = 0.0
            while t < secs:
                lv = (0.35 + 0.5 * rng.random() * abs(np.sin(time.monotonic() * 13))) if voiced else 0.04
                self.emit("level", float(lv))
                await asyncio.sleep(1 / 30)
                t += 1 / 30
        await level_for(0.55, False)
        words = text.split()
        for i, w in enumerate(words):
            self.emit("user", {"text": " ".join(words[:i + 1]), "partial": True})
            dur = (0.55 + 0.09 * len(w)) / 3.1 + (0.18 if w.endswith((",", ".")) else 0)
            await level_for(dur, True)
        await level_for(0.6, False)                              # endpoint silence
        self._span_begin()
        self.emit("user", {"text": text, "partial": False})
        self._set_state("thinking", "Thinking")
        self._turn = asyncio.create_task(self._respond(text))

    async def _barge_in(self, onset_t: float = 0.0) -> None:
        if self.state not in ("thinking", "speaking"):
            return
        await self._cancel_turn()
        self._begin_listen(heard=True, onset_t=onset_t)

    async def _interrupt_to_idle(self) -> None:
        await self._cancel_turn()
        self._set_state("idle", "")

    # ── reasoning + speech ───────────────────────────────────────
    def _system(self, hint: str = "") -> dict:
        v = self.vehicle
        now = dt.datetime.now()
        name = self.cfg.get("ASSISTANT_NAME", "Aria")
        units = "metric (km, °C)" if self.cfg.metric else "imperial (miles, °F)"
        return {"role": "system", "content": (
            f"You are {name}, the voice assistant built into a {v.name}. You are talking to the driver "
            f"({self.cfg.get('DRIVER_NAME', 'Driver')}) while they drive.\n"
            f"Now: {now.strftime('%A %B %-d %Y, %-I:%M %p')}. Car location: {v.place} ({v.lat:.4f},{v.lon:.4f}). "
            f"Battery {v.battery_pct:.0f}% (~{v.range_mi:.0f} mi range). Cabin {v.cabin_temp_f:.0f}°F. Units: {units}.\n"
            "Rules:\n"
            "- Your words are spoken aloud. Reply in 1–2 short sentences (max ~35 words). No lists, markdown, "
            "URLs or emoji. Say numbers the way a person would.\n"
            "- The screen shows rich cards from tools, so don't read every detail aloud; give the headline and the "
            "one thing that matters.\n"
            "- Use tools for anything about places, routes, charging, weather, current events or facts that may "
            "have changed. Never invent places, prices, hours or scores.\n"
            "- For trips use plan_trip; for 'take me to'/'navigate' use navigate_to; for nearby things use "
            "find_places; for general questions use web_search.\n"
            "- Keep the driver safe: no long readouts, no video, no texting while driving."
            + "".join("\n" + line for line in self.system_extra) + (("\n" + hint) if hint else ""))}

    async def _respond(self, user_text: str) -> None:
        if await self._intercept(user_text):
            if self.state in ("thinking", "speaking", "listening"):
                self._set_state("idle", "")
            return
        self.messages.append({"role": "user", "content": user_text})
        self.messages = self.messages[-(self.HISTORY * 4 if self.memory else self.HISTORY):]
        while self.messages and self.messages[0]["role"] == "tool":       # never start on orphan tool msgs
            self.messages.pop(0)
        turn_idx = len(self.messages) - 1
        card_before = self.last_card
        if not self.llm:
            self.emit("toast", "No LLM configured — add a key to .env")
            self._set_state("idle", "")
            return
        if self._spans is None:
            self._span_begin()
        spans = self._spans
        self._spoken = ""
        self._speak_q = asyncio.Queue()
        self._speaker = asyncio.create_task(self._speak_loop(self._speak_q))
        shown = ""
        cancelled = False
        self.emit("card", None)
        hint = ""
        outcomes, subjects, t0, merged = [], {}, time.monotonic(), False
        metric = bool(getattr(self.cfg, "metric", False))
        try:
            plan = await self.planner.before(user_text) if self.planner else None
        except Exception:                                            # noqa: BLE001
            log.exception("planner failed")
            plan = None
        compound = bool(plan and plan.kind == "compound")
        if compound:
            hint = COMPOUND_HINT
        needs, forced, nudged, self._cant = await self._needs_tool(user_text, plan), False, False, False
        self._turn_outcomes = outcomes
        self._cant_call = rules_call(user_text, tool_names(self.tools.schemas))
        self._vet_first = True
        try:
            if plan and plan.rule_actions:
                calls = [{"id": f"rule{i}", "name": a.name, "arguments": json.dumps(a.args)}
                         for i, a in enumerate(plan.rule_actions)]
                self.emit("caption", self.tools.captions.get(calls[0]["name"], "Working"))
                self._set_state("thinking")
                done, failed = [], []
                for c, (result, card, nav) in await self._run_tools(calls):
                    args = json.loads(c["arguments"])
                    result = label_stale(stamp(result))
                    outcomes.append(outcome_from(c["name"], args, result, metric))
                    if isinstance(result, dict) and result.get("error"):
                        failed.append(f"Tried {c['name']}({args}) and it failed: {result['error']}. Tell the driver.")
                        continue
                    self.planner.after(c["name"], args)
                    done.append(f"{c['name']} {args}")
                    if card:
                        self.last_card = card
                        self.emit("card", card)
                    if nav:
                        self.nav_open = True
                        self.emit("nav", nav)
                    if c["name"] == "set_climate":
                        self.emit("vehicle", self.vehicle.snapshot())
                lines = list(failed)
                if done:
                    lines.insert(0, "Already done this turn, do not call again: " + "; ".join(done)
                                 + ". Mention it briefly and handle the rest.")
                hint = "\n".join(([hint] if compound else []) + lines)
            for _round in range(self.MAX_ROUNDS):
                buf = ""
                text_this_round = ""
                calls = None
                held = None
                if needs and not outcomes and not forced:
                    held = ""                                # hold text until we know a tool is called
                budget = self.cfg.num("TURN_BUDGET_S", 6)
                llm_t0, llm_first, usage = time.monotonic(), None, None
                try:
                    async with asyncio.timeout(max(0.0, budget - (time.monotonic() - t0)) if compound else None):
                        async for ev in self.llm.stream([self._system(hint)] + self.messages, self.tools.schemas):
                            if ev[0] in ("text", "tool_calls"):
                                self._mark("first_token")
                                if llm_first is None:
                                    llm_first = time.monotonic() - llm_t0
                            if ev[0] == "usage":
                                usage = ev[1]
                            elif ev[0] == "text" and held is not None:
                                held += ev[1]
                                text_this_round += ev[1]
                            elif ev[0] == "text":
                                buf += ev[1]
                                text_this_round += ev[1]
                                shown = (shown + ev[1]) if shown else ev[1].lstrip()
                                self.emit("reply", clean_for_speech(shown))
                                buf = await self._flush_sentences(buf)
                            elif ev[0] == "tool_calls":
                                calls = ev[1]
                except TimeoutError:
                    merged = True
                    await self._speak_merge(outcomes, subjects)
                    break
                finally:
                    self._record_llm(usage, llm_first, time.monotonic() - llm_t0)
                if nudged and NUDGE in hint:
                    hint = hint.replace(NUDGE, "").strip()
                if held is not None and not calls:
                    cant = cant_first(held)
                    if not nudged:
                        nudged, hint = True, (hint + "\n" + NUDGE).strip()
                        log.info("tool-call guarantee: no tool call for %r, nudging", user_text)
                        continue
                    forced = True
                    if self._cant_call:
                        log.info("tool-call guarantee: rules path %s", self._cant_call[0])
                        if cant or not held.strip():
                            await self._speak_q.put(LET_ME_TRY)
                        calls = [{"id": "rules-guarantee", "name": self._cant_call[0],
                                  "arguments": json.dumps(self._cant_call[1])}]
                        held, text_this_round = None, ""
                    else:
                        held = ASK_PLACE if cant or not held.strip() else held
                        text_this_round = held
                if held is not None:
                    shown = (shown + held) if shown else held.lstrip()
                    self.emit("reply", clean_for_speech(shown))
                    buf = await self._flush_sentences(held)
                if buf.strip() and not self._cant:
                    await self._speak_q.put(buf)
                if self._cant and not calls and self._cant_call and not forced and not outcomes:
                    forced, self._cant = True, False
                    calls = [{"id": "rules-guarantee", "name": self._cant_call[0],
                              "arguments": json.dumps(self._cant_call[1])}]
                    text_this_round = LET_ME_TRY
                if not calls:
                    self.messages.append({"role": "assistant", "content": text_this_round})
                    if (compound or forced) and not self._spoken.strip() and not shown.strip():
                        merged = True
                        await self._speak_merge(outcomes, subjects)
                    break
                # tool round
                self.messages.append({"role": "assistant", "content": text_this_round or None, "tool_calls": [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                    for c in calls]})
                self.emit("caption", self.tools.captions.get(calls[0]["name"], "Working"))
                if self.state != "speaking":
                    self._set_state("thinking")
                dup = [c for c in calls if self.planner and self._already_done(c)]
                todo = [c for c in calls if c not in dup]
                for c in dup:
                    self.messages.append({"role": "tool", "tool_call_id": c["id"], "content": tool_result_json(
                        {"ok": True, "already_done": True})})
                for c, (result, card, nav) in await self._run_tools(todo):
                    args = self._args(c)
                    result = label_stale(stamp(result))
                    subjects[c["name"]] = str(args.get("query") or args.get("category") or "")
                    outcomes.append(outcome_from(c["name"], args, result, metric))
                    if self.planner:
                        self.planner.after(c["name"], self._args(c))
                    self.messages.append({"role": "tool", "tool_call_id": c["id"], "content": tool_result_json(result)})
                    if card:
                        self.last_card = card
                        self.emit("card", card)
                    if nav:
                        self.nav_open = True
                        self.emit("nav", nav)
                    if c["name"] == "set_climate":
                        self.emit("vehicle", self.vehicle.snapshot())
                self._cant, self._vet_first = False, False
                if self.state == "thinking":
                    self.emit("caption", "Thinking")
                if shown:
                    shown += " "
                if compound and time.monotonic() - t0 > self.cfg.num("TURN_BUDGET_S", 6):
                    merged = True
                    await self._speak_merge(outcomes, subjects)
                    break
            else:
                if (compound or forced) and not merged and not shown.strip() and not self._spoken.strip():
                    await self._speak_merge(outcomes, subjects)
            await self._speak_q.put(None)
            await self._speaker
        except asyncio.CancelledError:
            cancelled = True
            # keep what was actually said so the model knows it was cut off
            said = self._spoken.strip()
            note = (said + " …(interrupted by the driver)") if said else "(interrupted by the driver)"
            last = self.messages[-1] if self.messages else {}
            if last.get("role") == "assistant" and not last.get("tool_calls"):
                last["content"] = note                     # replace the full reply with what was heard
            else:
                self.messages.append({"role": "assistant", "content": note})
            raise
        except Exception as e:                                       # noqa: BLE001
            log.exception("turn failed")
            if self._speaker and not self._speaker.done():
                self._speaker.cancel()
            self.emit("toast", f"{type(e).__name__}: {str(e)[:120]}")
            self.messages.append({"role": "assistant", "content": "(error)"})
        finally:
            self._spans = None if self._spans is spans else self._spans
            self._mark_on(spans, "done")
            if self.memory:
                try:
                    self.memory.record_turn(turn_idx, self.last_card if self.last_card is not card_before else None)
                except Exception:
                    log.exception("could not record the turn")
            if not cancelled and self.state in ("thinking", "speaking"):
                self._set_state("idle", "")

    def _mark_on(self, spans, name: str) -> None:
        keep, self._spans = self._spans, spans
        try:
            self._mark(name)
        finally:
            self._spans = keep

    def _record_llm(self, usage: dict | None, first_s: float | None, total_s: float) -> None:
        u = usage or {}
        self._tel_call("llm", getattr(self.llm, "model", ""), getattr(self.llm, "provider", ""),
                       int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0),
                       float(u.get("cost") or 0.0), first_s, total_s)

    async def _speak_merge(self, outcomes: list, subjects: dict) -> None:
        reply = merge_reply(outcomes, subjects)
        if reply:
            self.emit("reply", reply)
            self.messages.append({"role": "assistant", "content": reply})
            await self._speak_q.put(reply)

    @staticmethod
    def _args(call: dict) -> dict:
        try:
            a = json.loads(call["arguments"] or "{}")
        except json.JSONDecodeError:
            return {}
        return a if isinstance(a, dict) else {}

    def _already_done(self, call: dict) -> bool:
        return self.planner.seen(call["name"], self._args(call))

    async def _needs_tool(self, text: str, plan) -> bool:
        if plan and plan.kind != "chat":
            return True
        try:
            return await needs_tool(text)
        except Exception:                                            # noqa: BLE001
            return False

    def _vet(self, sentence: str) -> str:
        """Replace a first-sentence 'can't do that' when no tool failed this turn; drop the rest of that reply."""
        if self._cant:
            return ""
        if self._vet_first and not self._turn_outcomes:
            self._vet_first = False
            if (self._cant_call and cant_first(sentence)
                    and not (self.planner and self.planner.seen(*self._cant_call))):
                self._cant = True
                return LET_ME_TRY
        return sentence

    async def _flush_sentences(self, buf: str) -> str:
        """Queue every complete sentence in buf (merging very short ones); return the rest."""
        pos = 0
        while True:
            m = SENT_BOUNDARY.search(buf, pos)
            if not m:
                break
            cut = m.end()
            if len(buf[:cut].strip()) < 14:          # "Sure." — wait and merge with the next
                pos = cut
                continue
            if out := self._vet(buf[:cut]):
                await self._speak_q.put(out)
            buf, pos = buf[cut:], 0
        if len(buf) > 220 and "," in buf[60:]:        # very long clause: split at last comma
            i = buf.rfind(",") + 1
            if out := self._vet(buf[:i]):
                await self._speak_q.put(out)
            buf = buf[i:]
        return buf

    async def _speak_loop(self, q: asyncio.Queue) -> None:
        """Synthesise sentences in order; synthesis of N+1 overlaps playback of N."""
        pending: list[asyncio.Task] = []
        silent_until = 0.0
        try:
            while True:
                sentence = await q.get()
                if sentence is None:
                    break
                text = clean_for_speech(sentence)
                if not text:
                    continue
                stream = self._synth_stream(text)
                if stream is not None:
                    start = await self._speak_stream(stream, text, pending)
                    silent_until = max(silent_until, start)
                    continue
                speech = await self._synth(text)
                self._mark("tts_first_chunk")
                if speech.pcm16:
                    start = self.player.enqueue(speech.pcm16)
                    self._mark("first_audio", at=start)
                else:
                    start = max(time.monotonic(), silent_until)
                    silent_until = start + speech.duration
                if self.state != "speaking":
                    self._set_state("speaking", "")
                self._spoken += " " + text
                self.emit("words", [(w, start + s, d) for w, s, d in speech.words])
                pending = [t for t in pending if not t.done()]
                pending.append(asyncio.ensure_future(self._level_until(start, start + speech.duration)))
            # drain
            while self.player.playing or any(not t.done() for t in pending):
                await asyncio.sleep(0.05)
        finally:
            for t in pending:
                t.cancel()

    def _synth_stream(self, text: str):
        fn = getattr(self.tts, "synth_stream", None) if self.tts else None
        return fn(text) if fn else None

    async def _speak_stream(self, stream, text: str, pending: list) -> float:
        """Enqueue chunks as they are generated; returns the playback end time of the sentence."""
        end = 0.0
        first = True
        try:
            async for chunk in stream:
                self._mark("tts_first_chunk")
                if chunk.pcm16:
                    start = self.player.enqueue(chunk.pcm16)
                    self._mark("first_audio", at=start)
                else:
                    start = end
                end = start + chunk.duration
                if first:
                    first = False
                    if self.state != "speaking":
                        self._set_state("speaking", "")
                    self._spoken += " " + text
                self.emit("words", [(w, start + s, d) for w, s, d in chunk.words])
                pending[:] = [t for t in pending if not t.done()]
                if chunk.pcm16:
                    pending.append(asyncio.ensure_future(self._level_until(start, end)))
        except Exception as e:                                       # noqa: BLE001
            if not first:
                log.warning("tts stream failed: %s", e)
                return end
            log.warning("tts failed: %s", e)
            self.emit("toast", f"Voice unavailable: {str(e)[:60]}")
            self.tts = None
            dur = max(0.8, len(text.split()) / 2.7)
            start = time.monotonic()
            self._set_state("speaking", "")
            self._spoken += " " + text
            sp = _SilentSpeech(text, dur)
            self.emit("words", [(w, start + s, d) for w, s, d in sp.words])
            pending.append(asyncio.ensure_future(self._level_until(start, start + dur)))
            return start + dur
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose:
                await aclose()
        return end

    async def _synth(self, text: str) -> Speech:
        if self.tts:
            try:
                return await self.tts.synth(text)
            except Exception as e:                                   # noqa: BLE001
                log.warning("tts failed: %s", e)
                self.emit("toast", f"Voice unavailable: {str(e)[:60]}")
                self.tts = None
        # silent speech: the avatar still talks at a natural pace
        dur = max(0.8, len(text.split()) / 2.7)
        return _SilentSpeech(text, dur)

    async def _level_until(self, start: float, end: float) -> None:
        """Publish the playback envelope (or a synthetic one for silent speech)."""
        await asyncio.sleep(max(0.0, start - time.monotonic()))
        t0 = time.monotonic()
        while time.monotonic() < end:
            if self.player.playing:
                lv = float(np.clip((self.player.level_db + 50) / 38, 0, 1))
            else:
                t = time.monotonic() - t0
                lv = 0.35 + 0.35 * abs(np.sin(t * 2 * np.pi * 2.3))
            self.emit("level", lv)
            await asyncio.sleep(1 / 30)

    def _write_ics(self, p: dict) -> Path:
        start = dt.datetime.now().replace(second=0, microsecond=0) + dt.timedelta(minutes=30)
        end = start + dt.timedelta(seconds=float(p.get("duration_s", 3600)))
        f = lambda t: t.strftime("%Y%m%dT%H%M%S")
        out = Path.home() / "Downloads"
        out = out if (out.exists() and not self.sim) else self.cache
        path = out / f"{re.sub(r'[^A-Za-z0-9]+', '_', p.get('title', 'trip'))[:40]}.ics"
        path.write_text("\r\n".join([
            "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Elevatics//Cabin Assistant//EN", "BEGIN:VEVENT",
            f"UID:{int(time.time())}@cabin", f"DTSTAMP:{f(dt.datetime.now())}", f"DTSTART:{f(start)}",
            f"DTEND:{f(end)}", f"SUMMARY:{p.get('title', 'Trip')}", f"LOCATION:{p.get('location', '')}",
            f"DESCRIPTION:{p.get('when', '')}", "END:VEVENT", "END:VCALENDAR", ""]))
        return path


class _SilentSpeech(Speech):
    def __init__(self, text: str, dur: float) -> None:
        super().__init__(b"", estimate_words(text, dur))
        self._dur = dur

    @property
    def duration(self) -> float:
        return self._dur
