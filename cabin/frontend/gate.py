"""Only the driver starts a turn: a direction-of-arrival zone and a wake phrase in front of the VAD.

``DriverGateStage`` sits between the residual gate and the VAD. With an XVF3800 it asks the board
where the voice comes from (``XvfDoa``); without one it checks that the frame is well above the
cabin noise floor, which a driver at the microphone is and a passenger across the cabin is not.
``WakeStage`` sits behind the VAD: on ``start`` it holds the event back, transcribes the first
``WAKE_WINDOW_S`` of the utterance once, and lets the turn through only when it begins with the
wake phrase.

Settings: ``DRIVER_GATE`` auto|doa|near_field|off, ``DRIVER_BEARING_DEG``, ``DRIVER_ZONE_DEG``,
``NEAR_FIELD_DB``, ``WAKE_WORD``, ``WAKE_WINDOW_S``.
"""
from __future__ import annotations

import logging
import math
import struct
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from ..audio import to_pcm16
from ..providers.wake import WakeMatcher, split_phrases

log = logging.getLogger("frontend.gate")

FRAME_S = 0.02
VID, PID = 0x2886, 0x001A
_REQUEST_IN = 0xC0                  # device-to-host | vendor | device
_BUSY = 64
_RESID, _CMDID, _COUNT = 35, 11, 2  # AUDIO_MGR_SELECTED_AZIMUTHS: processed DoA, auto-beam DoA (radians)
CTRL_TIMEOUT_MS = 50
POLL_HZ = 10.0
DOA_MAX_AGE_S = 0.6
RING_FRAMES = 150


def _db(x: np.ndarray) -> float:
    return 10.0 * math.log10(float(np.mean(np.square(x, dtype=np.float64))) + 1e-12)


def _angle_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


class DriverGate:
    """``allow`` says whether a frame can be the driver's voice."""

    def __init__(self, bearing_deg: float = 0.0, zone_deg: float = 40.0, near_field_db: float = 18.0) -> None:
        self.bearing_deg = bearing_deg
        self.zone_deg = zone_deg
        self.near_field_db = near_field_db

    def allow(self, frame: np.ndarray, doa_deg: float | None, noise_floor_db: float) -> bool:
        if doa_deg is not None:
            return _angle_diff(doa_deg, self.bearing_deg) <= self.zone_deg
        return _db(frame) - noise_floor_db >= self.near_field_db


class XvfDoa:
    """Direction of arrival from the XVF3800 over its USB control endpoint (read-only).

    A background thread polls at most ``POLL_HZ``; ``read`` returns the latest bearing in degrees
    (0..360) or None when the board is absent, silent, or the reading is stale. Needs pyusb and
    the udev rule that lets the user open the device.
    """

    def __init__(self, *, poll_hz: float = POLL_HZ, find: Callable[[], object] | None = None) -> None:
        self._find = find or self._find_usb
        self._period = 1.0 / min(poll_hz, POLL_HZ)
        self._dev = None
        self._deg: float | None = None
        self._at = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error = ""

    @staticmethod
    def _find_usb():
        import usb.core
        return usb.core.find(idVendor=VID, idProduct=PID)

    @property
    def present(self) -> bool:
        return self._dev is not None

    def open(self) -> bool:
        try:
            self._dev = self._find()
        except Exception as e:                         # noqa: BLE001  (no pyusb, no permission, no board)
            self.error = f"{type(e).__name__}: {e}"
            self._dev = None
        else:
            if self._dev is None:
                self.error = "no XVF3800 on USB"
        return self._dev is not None

    def poll_once(self) -> float | None:
        dev = self._dev
        if dev is None:
            return None
        length = _COUNT * 4 + 1
        deadline = time.monotonic() + CTRL_TIMEOUT_MS / 1000
        deg = None
        try:
            while True:
                reply = bytes(dev.ctrl_transfer(_REQUEST_IN, 0, 0x80 | _CMDID, _RESID, length, CTRL_TIMEOUT_MS))
                if reply and reply[0] == _BUSY and time.monotonic() < deadline:
                    time.sleep(0.002)
                    continue
                if reply and reply[0] == 0 and len(reply) >= length:
                    az = struct.unpack("<2f", reply[1:length])[0]
                    if math.isfinite(az):
                        deg = math.degrees(az) % 360.0
                break
        except Exception as e:                         # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            return None
        if deg is not None:
            with self._lock:
                self._deg, self._at = deg, time.monotonic()
        return deg

    def read(self) -> float | None:
        with self._lock:
            if self._deg is None or time.monotonic() - self._at > DOA_MAX_AGE_S:
                return None
            return self._deg

    def start(self) -> bool:
        if self._thread is not None:
            return True
        if not self.present and not self.open():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="xvf-doa", daemon=True)
        self._thread.start()
        return True

    def close(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=1.0)

    def _run(self) -> None:
        while not self._stop.wait(self._period):
            self.poll_once()


class FrameRing:
    """The last frames that reached the driver gate, in capture order (gated or not)."""

    def __init__(self, maxlen: int = RING_FRAMES) -> None:
        self._d: deque = deque(maxlen=maxlen)

    def append(self, frame: np.ndarray) -> None:
        self._d.append(np.array(frame, np.float32))

    def tail(self, n: int) -> np.ndarray:
        frames = list(self._d)[-n:] if n > 0 else []
        return np.concatenate(frames) if frames else np.zeros(0, np.float32)

    def clear(self) -> None:
        self._d.clear()


class DriverGateStage:
    """Drops frames that cannot be the driver's voice; a turn already running is left alone.

    Frames after a driver frame pass for ``hold_frames`` so the VAD sees the pause that ends the
    turn. ``mode`` is doa, near (near-field level), auto (doa when the board answers) or off.
    """

    def __init__(self, gate: DriverGate, doa: XvfDoa | None = None, mode: str = "auto", *,
                 hold_frames: int = 20, in_speech: Callable[[], bool] | None = None,
                 warmup_frames: int = 25, ring: FrameRing | None = None) -> None:
        self.gate = gate
        self.doa = doa
        self.mode = mode
        self.hold_frames = hold_frames
        self._in_speech = in_speech
        self._warmup = warmup_frames
        self.ring = ring
        self._seen = 0
        self._hold = 0
        self._floor: float | None = None

    def close(self) -> None:
        if self.doa is not None:
            self.doa.close()

    def reset(self) -> None:
        self._hold = 0

    def effective_mode(self) -> str:
        if self.mode == "auto":
            return "doa" if self.doa is not None and self.doa.present else "near"
        return self.mode

    def _track_floor(self, frame: np.ndarray) -> float:
        p = _db(frame)
        if self._floor is None or p < self._floor:
            self._floor = p if self._floor is None else self._floor + 0.1 * (p - self._floor)
        else:
            self._floor += 0.002 * (p - self._floor)
        return self._floor

    def process(self, frame: np.ndarray, ctx) -> np.ndarray | None:
        if self.ring is not None:
            self.ring.append(frame)
        mode = self.effective_mode()
        ctx.stats["driver_gate"] = mode
        if mode == "off":
            return frame
        floor = self._track_floor(frame)
        self._seen += 1
        if mode == "doa":
            doa = self.doa.read() if self.doa is not None else None
            ctx.stats["doa_deg"] = None if doa is None else round(doa, 1)
            ok = doa is not None and self.gate.allow(frame, doa, floor)
        else:
            ok = self._seen <= self._warmup or self.gate.allow(frame, None, floor)
        if ok:
            self._hold = self.hold_frames
            return frame
        if self._hold > 0:
            self._hold -= 1
            return frame
        if self._in_speech is not None and self._in_speech():
            return frame
        return None


class _GatedVad:
    """What the residual gate replays held frames into: through the driver gate, then the VAD."""

    def __init__(self, gate_stage: DriverGateStage, vad) -> None:
        self._gate, self._vad = gate_stage, vad

    def process(self, frame: np.ndarray, ctx) -> np.ndarray | None:
        x = self._gate.process(frame, ctx)
        return None if x is None else self._vad.process(x, ctx)

    def reset(self) -> None:
        self._vad.reset()


class WakeStage:
    """Holds a ``start`` until the first ``window_s`` of speech has been checked for the wake phrase.

    The transcription runs on a one-slot executor thread, so frames keep flowing while it works;
    its verdict is applied on the first frame after it finishes. An ``end`` that arrives meanwhile
    waits for the verdict. The released ``start`` carries ``onset_t``, the capture time of the
    frame the VAD started on.
    """

    PRE_FRAMES = 15                     # audio from just before the VAD fired

    def __init__(self, matcher: WakeMatcher, ring: FrameRing, transcribe: Callable[[bytes], str | None],
                 required: Callable[[], bool], window_s: float = 1.2) -> None:
        self.matcher = matcher
        self.ring = ring
        self._transcribe = transcribe
        self._required = required
        self.window_frames = max(1, round(window_s / FRAME_S))
        self._pool: ThreadPoolExecutor | None = None
        self._collecting = None         # start event while the window fills
        self._after = 0
        self._job: tuple | None = None  # (future, start event)
        self._held_end = None
        self._swallow_end = False
        self._warned = False

    def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def reset(self) -> None:
        self._collecting = None
        self._job = None
        self._held_end = None
        self._swallow_end = False

    def process(self, frame: np.ndarray, ctx) -> np.ndarray | None:
        out = []
        for ev in ctx.events:
            if ev.kind == "start":
                if self._collecting is None and self._job is None and self.matcher and self._required():
                    self._collecting, self._after = ev, 0
                elif self._collecting is None and self._job is None:
                    out.append(ev)
                else:
                    self._swallow_end = True                   # a turn is already being judged
            elif ev.kind == "end":
                if self._collecting is not None:
                    self._submit(ctx)
                    self._held_end = ev
                elif self._job is not None:
                    self._held_end = ev
                elif self._swallow_end:
                    self._swallow_end = False
                else:
                    out.append(ev)
            else:
                out.append(ev)
        ctx.events[:] = out
        if self._collecting is not None:
            self._after += 1
            if self._after >= self.window_frames:
                self._submit(ctx)
        if self._job is not None and self._job[0].done():
            ctx.events.extend(self._finish(ctx))
        return frame

    def _submit(self, ctx) -> None:
        start, self._collecting = self._collecting, None
        pcm = to_pcm16(self.ring.tail(self.PRE_FRAMES + self._after))
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wake")
        self._job = (self._pool.submit(self._run, pcm), start)

    def _run(self, pcm: bytes) -> str | None:
        try:
            return self._transcribe(pcm)
        except Exception:
            log.exception("wake transcription failed")
            return None

    def _finish(self, ctx) -> list:
        (fut, start), self._job = self._job, None
        end, self._held_end = self._held_end, None
        text = fut.result()
        ok, rest = True, ""
        if text is None:
            if not self._warned:
                self._warned = True
                log.warning("wake phrase cannot be checked (no transcriber); letting the turn through")
            ctx.stats["wake"] = "unchecked"
        else:
            ok, rest = self.matcher.match(text)
            ctx.stats["wake"] = "match" if ok else "drop"
        if not ok:
            self._swallow_end = self._swallow_end or end is None
            return []
        from .worker import FrontEvent
        events = [FrontEvent("wake", ctx.t, start.level, rest, ctx.seq),
                  FrontEvent("start", start.t, start.level, "", start.seq, onset_t=start.t)]
        if end is not None:
            events.append(end)
        return events


def install_gate(front_end, *, transcribe=None, required=None, doa: XvfDoa | None = None) -> None:
    """Add the driver gate behind the residual gate and the wake stage behind the VAD stage.

    Front ends without a VAD stage are left alone. ``transcribe`` and ``required`` default to the
    ``cabin.frontend`` hooks the frontend plugin sets.
    """
    from .. import audio, frontend
    from .aec import ResidualGateStage
    cfg = front_end.cfg
    stages = front_end.stages
    at = next((i for i, s in enumerate(stages) if isinstance(s, audio.VadStage)), None)
    if at is None or any(isinstance(s, DriverGateStage) for s in stages):
        return
    mode = cfg.get("DRIVER_GATE", "off").strip().lower().replace("-", "_")
    mode = {"near_field": "near", "nearfield": "near"}.get(mode, mode)
    if mode not in ("auto", "doa", "near", "off"):
        mode = "off"
    phrases = split_phrases(cfg.get("WAKE_WORD"))
    front_end.stats.update(driver_gate=mode if mode == "off" else "pending", wake=bool(phrases))
    if mode == "off" and not phrases:
        return
    vad = stages[at]
    owner = getattr(getattr(vad, "_detect", None), "__self__", None)
    in_speech = (lambda: bool(getattr(owner, "in_speech", False))) if owner is not None else None
    if mode in ("auto", "doa") and doa is None:
        doa = XvfDoa()
        if not doa.start():
            log.info("XVF3800 direction of arrival unavailable (%s); using the near-field level", doa.error)
    ring = FrameRing()
    gate = DriverGate(cfg.num("DRIVER_BEARING_DEG", 0.0), abs(cfg.num("DRIVER_ZONE_DEG", 40.0)),
                      cfg.num("NEAR_FIELD_DB", 18.0))
    gate_stage = DriverGateStage(gate, doa, mode, in_speech=in_speech, ring=ring)
    for s in stages:
        if isinstance(s, ResidualGateStage) and s._vad is vad:
            s._vad = _GatedVad(gate_stage, vad)
    stages.insert(at, gate_stage)
    if phrases:
        stages.insert(at + 2, WakeStage(
            WakeMatcher(phrases), ring,
            transcribe or (lambda pcm: _hook_transcribe(frontend, pcm)),
            required or (lambda: frontend.wake_required()),
            cfg.num("WAKE_WINDOW_S", 1.2)))


def _hook_transcribe(frontend, pcm: bytes) -> str | None:
    fn = frontend.wake_transcriber()
    return None if fn is None else fn(pcm)
