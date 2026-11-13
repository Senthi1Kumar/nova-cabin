"""Audio I/O: microphone capture with VAD, and a low-latency speech player.

Threads: PortAudio callbacks run on their own threads; everything they hand
over is lock-protected or goes through `call_soon_threadsafe`.

Mic  → 16 kHz mono int16 frames of 20 ms → VAD → engine callbacks
Play ← 24 kHz mono int16 chunks (one per sentence) → device rate, with an
       output envelope (for the UI and the avatar) and start-time reporting
       (for lip sync).
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("audio")

MIC_SR = 16000
TTS_SR = 24000
FRAME_MS = 20
FRAME = MIC_SR * FRAME_MS // 1000

try:                                   # PortAudio is optional (headless / CI)
    import sounddevice as sd
    _SD_ERR = None
except Exception as e:                 # noqa: BLE001
    sd = None
    _SD_ERR = str(e)


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """Band-limited enough for speech; numpy only."""
    if sr_in == sr_out or len(x) == 0:
        return x.astype(np.float32)
    x = x.astype(np.float32)
    if sr_out < sr_in:                 # simple windowed-sinc low-pass before decimation
        cutoff = 0.5 * sr_out / sr_in * 0.9
        n = 31
        t = np.arange(n) - (n - 1) / 2
        h = np.sinc(2 * cutoff * t) * np.hamming(n)
        h /= h.sum()
        x = np.convolve(x, h, mode="same")
    n_out = round(len(x) * sr_out / sr_in)
    return np.interp(np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x).astype(np.float32)


def rms_db(x: np.ndarray) -> float:
    r = float(np.sqrt(np.mean(np.square(x, dtype=np.float64)) + 1e-12))
    return 20 * np.log10(r + 1e-9)


# ─────────────────────────────────────────────────────────────────────
#  Voice activity detection — adaptive noise floor, hysteresis, echo-aware
# ─────────────────────────────────────────────────────────────────────
@dataclass
class VADConfig:
    sensitivity: float = 0.6           # 0..1
    start_ms: int = 160                # sustained speech before onset
    end_silence_ms: int = 700
    min_speech_ms: int = 300
    preroll_ms: int = 300
    max_utterance_s: float = 20.0


class EnergyVAD:
    def __init__(self, cfg: VADConfig) -> None:
        self.cfg = cfg
        self.noise_db = -55.0
        self.echo_gain_db = -20.0      # learned: mic level relative to playback level
        self.reset()

    def reset(self) -> None:
        self.in_speech = False
        self.run_on = 0
        self.run_off = 0
        self.speech_frames = 0

    def threshold(self, playback_db: float | None) -> float:
        margin = 18 - 10 * self.cfg.sensitivity             # 8..18 dB above floor
        th = self.noise_db + margin
        if playback_db is not None and playback_db > -60:   # assistant talking: lift over echo
            th = max(th, playback_db + self.echo_gain_db + 9)
        return th

    def update(self, frame: np.ndarray, playback_db: float | None) -> tuple[str | None, float]:
        """Returns (event, level 0..1). event: 'start' | 'end' | None."""
        db = rms_db(frame)
        th = self.threshold(playback_db)
        voiced = db > th
        # adapt noise floor only on unvoiced frames, quick down, slow up
        if not voiced and not self.in_speech:
            a = 0.2 if db < self.noise_db else 0.01
            self.noise_db += (db - self.noise_db) * a
            self.noise_db = min(max(self.noise_db, -75), -25)
        # learn echo coupling while playing and nobody talks
        if playback_db is not None and playback_db > -50 and not self.in_speech:
            est = db - playback_db
            self.echo_gain_db += (est - self.echo_gain_db) * (0.05 if est > self.echo_gain_db else 0.005)
        level = float(np.clip((db - self.noise_db) / 40.0, 0, 1))

        ev = None
        f = FRAME_MS
        if voiced:
            self.run_on += f
            self.run_off = 0
        else:
            self.run_off += f
            self.run_on = 0
        if not self.in_speech and self.run_on >= self.cfg.start_ms:
            self.in_speech = True
            self.speech_frames = self.run_on // f
            ev = "start"
        elif self.in_speech:
            self.speech_frames += 1
            if self.run_off >= self.cfg.end_silence_ms:
                self.in_speech = False
                ev = "end"
        return ev, level


class VadStage:
    """Front-end stage wrapping a synchronous ``(frame, playback_db) -> (event, level)`` detector."""

    def __init__(self, detect: Callable[[np.ndarray, float | None], tuple[str | None, float]],
                 reset: Callable[[], None] | None = None) -> None:
        self._detect = detect
        self._reset = reset

    def process(self, frame: np.ndarray, ctx) -> np.ndarray | None:
        ev, level = self._detect(frame, ctx.playback_db)
        ctx.emit("level", level)
        if ev:
            ctx.emit(ev, level)
        return frame

    def reset(self) -> None:
        if self._reset is not None:
            self._reset()


class VAD(EnergyVAD):
    """Engine-facing VAD. Energy-based by default; delegates to Silero + Smart Turn when
    ``VAD_ENGINE`` is ``silero`` (or ``auto`` and the ONNX models are present).

    With ``FRONTEND_THREAD`` on (default) and a running event loop, detection runs on the
    front-end worker thread: ``update`` forwards the frame and returns the oldest event the worker
    has produced so far (at most a frame or two late). Without a loop, or with the flag off,
    ``update`` is synchronous.
    """

    def __init__(self, cfg: VADConfig, engine: str | None = None, threaded: bool | None = None) -> None:
        self._frontend = None
        self._stopped = False
        self._events: collections.deque = collections.deque()
        self.last_onset_t = 0.0      # onset_t of the "start" returned by the latest update(); 0.0 if none
        self._last_level = 0.0
        super().__init__(cfg)
        self._turn = None
        self.engine = "energy"
        from .config import Settings
        self._settings = settings = Settings.load()
        if engine is None:
            engine = settings.get("VAD_ENGINE", "auto")
        self._threaded = settings.flag("FRONTEND_THREAD", True) if threaded is None else threaded
        if engine.lower() in ("silero", "auto"):
            try:
                from .providers.turn import SileroTurn, models_present
                if engine.lower() == "silero" or models_present():
                    self._turn = SileroTurn(cfg, self)
                    self.engine = "silero"
            except Exception as e:                      # noqa: BLE001
                log.warning("silero turn-taking unavailable, using energy VAD: %s", e)

    def reset(self) -> None:
        fe = getattr(self, "_frontend", None)
        if fe is not None and fe.running:
            fe.reset()                  # the worker resets on its own thread
            self._events.clear()
            return
        self._reset_state()

    def _reset_state(self) -> None:
        super().reset()
        turn = getattr(self, "_turn", None)
        if turn is not None:
            turn.reset()

    def _update_sync(self, frame: np.ndarray, playback_db: float | None) -> tuple[str | None, float]:
        self.last_onset_t = 0.0
        ev, level = super().update(frame, playback_db)
        if self._turn is None:
            return ev, level
        return self._turn.update(frame, playback_db, ev, level)

    def _on_front_event(self, ev) -> None:
        fe = self._frontend
        if fe is not None and ev.seq < fe.reset_seq:
            return
        self._last_level = ev.level
        if ev.kind in ("start", "end"):
            self._events.append((ev.kind, ev.level, getattr(ev, "onset_t", 0.0)))

    def _ensure_worker(self) -> bool:
        fe = self._frontend
        if fe is not None:
            if fe.running:
                return True
            if not self._stopped:
                log.warning("front-end worker thread died; using synchronous VAD")
                self._threaded = False
                self._frontend = None
            return False
        if not self._threaded or self._stopped:
            return False
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        from .frontend import FrontEnd
        self._frontend = FrontEnd(self._settings, self._on_front_event,
                                  stages=[VadStage(self._update_sync, self._reset_state)])
        self._frontend.start()
        return True

    def start(self) -> None:
        """Allow the worker to run again after `stop()`; it starts on the next `update`."""
        self._stopped = False
        self._frontend = None

    def stop(self) -> None:
        self._stopped = True
        fe = self._frontend
        if fe is not None:
            fe.stop()

    def update(self, frame: np.ndarray, playback_db: float | None) -> tuple[str | None, float]:
        if not self._ensure_worker():
            return self._update_sync(frame, playback_db)
        self._frontend.push(frame, playback_db)
        try:
            kind, _level, self.last_onset_t = self._events.popleft()
            return kind, self._last_level
        except IndexError:
            return None, self._last_level


def make_vad(cfg: VADConfig, engine: str | None = None) -> VAD:
    return VAD(cfg, engine)


# ─────────────────────────────────────────────────────────────────────
#  Player
# ─────────────────────────────────────────────────────────────────────
# Called as fn(t_abs, rms) for every ~20 ms of output; t_abs is the monotonic time at which that
# audio becomes audible (same clock `Player.enqueue` returns). Runs on the audio thread.
position_listeners: list[Callable[[float, float], None]] = []
# Called on the audio thread with every 20 ms mic frame; listeners must be O(1) (append to a deque).
frame_listeners: list[Callable[[np.ndarray], None]] = []
POSITION_MS = 20


class _StreamResampler:
    """Continuous sample-rate conversion across blocks.

    The low-pass filter keeps its last taps between blocks and the fractional read position keeps
    accumulating, so a stream cut into arbitrary blocks comes out without holes or overlaps.
    """

    TAPS = 31

    def __init__(self, sr_in: int, sr_out: int) -> None:
        self.step = sr_in / sr_out
        self.h = None
        if sr_out < sr_in:
            cutoff = 0.5 * sr_out / sr_in * 0.9
            t = np.arange(self.TAPS) - (self.TAPS - 1) / 2
            h = np.sinc(2 * cutoff * t) * np.hamming(self.TAPS)
            self.h = (h / h.sum()).astype(np.float32)
        self.reset()

    def reset(self) -> None:
        self._carry = np.zeros(0 if self.h is None else self.TAPS - 1, np.float32)
        self._last = np.float32(0.0)   # filtered sample just before the current block
        self._g0 = 0                   # global input index of the first sample of the next block
        # next output position, in input samples; the filter output lags its input by half its length
        self._q = 0.0 if self.h is None else (self.TAPS - 1) / 2

    def feed(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, np.float32)
        if self.h is not None:
            ext = np.concatenate([self._carry, x])
            f = np.convolve(ext, self.h, mode="valid")
            self._carry = ext[len(ext) - (self.TAPS - 1):]
        else:
            f = x
        last_idx = self._g0 + len(f) - 1
        if len(f) == 0 or self._q > last_idx:
            self._g0 += len(f)
            return np.zeros(0, np.float32)
        n_out = int((last_idx - self._q) / self.step + 1e-9) + 1
        q = self._q + self.step * np.arange(n_out)
        fe = np.concatenate([[self._last], f])
        rel = q - (self._g0 - 1)
        i0 = np.minimum(rel.astype(np.int64), len(fe) - 1)
        fr = (rel - i0).astype(np.float32)
        y = fe[i0] * (1 - fr) + fe[np.minimum(i0 + 1, len(fe) - 1)] * fr
        self._q = self._q + self.step * n_out
        self._last = f[-1]
        self._g0 += len(f)
        return y.astype(np.float32)


class ReferenceTap:
    """What the speaker plays, as mono float32 at 16 kHz, indexed by the playback clock.

    Each block is written at the sample index of the time it becomes audible, so `read(n)` returns
    the last n samples aligned to "now" with zeros wherever nothing played. Blocks that follow each
    other on the playback clock are resampled as one stream.
    """

    SECONDS = 2.0

    def __init__(self, sr: int = MIC_SR) -> None:
        self.sr = sr
        self._cap = int(self.SECONDS * sr)
        self._ring = np.zeros(self._cap, np.float32)
        self._head = 0                 # one past the highest absolute sample index written
        self._lock = threading.Lock()
        self._rs: _StreamResampler | None = None
        self._rs_in = 0
        self._rs_next_t = -1.0         # playback time at which the next block of the stream starts
        self._rs_idx = 0               # ring index of the next output sample of the stream
        self.delay_fn: Callable[[], float] | None = None   # seconds between hand-off and speaker, if known

    def write(self, t: float, x: np.ndarray, sr: int) -> None:
        if not len(x):
            return
        with self._lock:
            contiguous = self._rs is not None and self._rs_in == sr and abs(t - self._rs_next_t) < 5e-4
            if not contiguous:
                if self._rs is None or self._rs_in != sr:
                    self._rs, self._rs_in = _StreamResampler(sr, self.sr), sr
                else:
                    self._rs.reset()
                self._rs_idx = round(t * self.sr)
            self._rs_next_t = t + len(x) / sr
            y = self._rs.feed(x)
            if not len(y):
                return
            start = self._rs_idx
            self._rs_idx += len(y)
            if start > self._head:     # silence between blocks
                self._zero(self._head, start)
            self._put(start, y)
            self._head = max(self._head, start + len(y))

    def _put(self, a: int, y: np.ndarray) -> None:
        n = len(y)
        if n >= self._cap:             # only the newest capacity worth is kept
            y = y[n - self._cap:]
            a += n - self._cap
            n = self._cap
        i = a % self._cap
        k = min(n, self._cap - i)
        self._ring[i:i + k] = y[:k]
        if k < n:
            self._ring[:n - k] = y[k:]

    def _zero(self, a: int, b: int) -> None:
        n = b - a
        if n >= self._cap:
            self._ring[:] = 0.0
            return
        i = a % self._cap
        k = min(n, self._cap - i)
        self._ring[i:i + k] = 0.0
        if k < n:
            self._ring[:n - k] = 0.0

    def read(self, n: int, t_end: float | None = None) -> np.ndarray:
        """The n samples ending at playback time `t_end` (monotonic seconds; default now)."""
        return self.read_at(round((time.monotonic() if t_end is None else t_end) * self.sr), n)

    def read_at(self, end: int, n: int) -> np.ndarray:
        out = np.zeros(n, np.float32)
        with self._lock:
            lo = max(end - n, self._head - self._cap)
            hi = min(end, self._head)
            if hi > lo:
                i = lo % self._cap
                k = min(hi - lo, self._cap - i)
                o = lo - (end - n)
                out[o:o + k] = self._ring[i:i + k]
                if k < hi - lo:
                    out[o + k:o + (hi - lo)] = self._ring[:hi - lo - k]
        return out


current_reference: ReferenceTap | None = None   # tap of the most recently created Player


class Player:
    """Sequential chunk player. `enqueue` returns the wall-clock start time.

    Playback time is counted in output samples from an anchor set when the queue starts, so the
    start times handed out by `enqueue` and the envelope reported through `on_position` share
    one clock that does not jitter with callback timing.
    """

    def __init__(self, device=None) -> None:
        self._lock = threading.Lock()
        self._buf: collections.deque[np.ndarray] = collections.deque()
        self._queued = 0               # samples (device rate) waiting
        self._env_db = -90.0
        self._stream = None
        self.sr = TTS_SR
        self.latency = 0.0
        try:
            self.gain = max(0.5, min(6.0, float(os.environ.get("PLAYBACK_GAIN", "2.2"))))
        except ValueError:
            self.gain = 2.2
        self.on_drained: Callable[[], None] | None = None
        self.on_position: Callable[[float, float], None] | None = None
        self._anchor = 0.0             # monotonic time at which sample 0 of the current run is audible
        self._consumed = 0             # samples output since the anchor
        self._win_sq = 0.0
        self._win_n = 0
        self._win_t = 0.0
        self._was_playing = False
        global current_reference
        self.reference = current_reference = ReferenceTap()
        self.reference.delay_fn = lambda: self.reference_delay_s
        muted = os.environ.get("NOVA_AUDIO_MUTE", "").strip().lower() in ("1", "true", "yes")
        if muted:
            log.info("audio output muted (NOVA_AUDIO_MUTE)")
        if sd is not None and not muted:
            try:
                info = sd.query_devices(device, "output")
                self.sr = int(info["default_samplerate"])
                self._stream = sd.OutputStream(samplerate=self.sr, channels=1, dtype="float32",
                                               device=device, blocksize=0, latency="low",
                                               callback=self._cb)
                self._stream.start()
                self.latency = float(self._stream.latency)
            except Exception as e:     # noqa: BLE001
                log.warning("output device unavailable: %s", e)
                self._stream = None
        if self._stream is None:       # headless: consume in real time
            self._null = threading.Thread(target=self._null_loop, daemon=True)
            self._null.start()

    @property
    def available(self) -> bool:
        return self._stream is not None

    @property
    def reference_delay_s(self) -> float:
        """Seconds between a sample being handed to the output and it leaving the speaker."""
        return self.latency

    def enqueue(self, pcm16: bytes | np.ndarray, sr: int = TTS_SR) -> float:
        x = np.frombuffer(pcm16, np.int16).astype(np.float32) / 32768.0 if isinstance(pcm16, bytes) else pcm16
        y = resample(x, sr, self.sr)
        if self.gain != 1.0:                       # small speakers: boost, soft-limit so peaks never clip
            y = np.tanh(y * self.gain / 0.9) * 0.9
        # 6 ms fades so chunk joins never click
        n = min(len(y) // 4, int(self.sr * 0.006))
        if n > 1:
            ramp = np.linspace(0, 1, n, dtype=np.float32)
            y[:n] *= ramp
            y[-n:] *= ramp[::-1]
        with self._lock:
            if self._queued <= 0:
                self._anchor = time.monotonic() + self.latency
                self._consumed = 0
            start = self._anchor + (self._consumed + self._queued) / self.sr
            self._buf.append(y)
            self._queued += len(y)
            self._was_playing = True
        return start

    def flush(self) -> None:
        with self._lock:
            self._buf.clear()
            self._queued = 0

    @property
    def playing(self) -> bool:
        with self._lock:
            return self._queued > 0

    @property
    def remaining_s(self) -> float:
        with self._lock:
            return self._queued / self.sr

    @property
    def level_db(self) -> float:
        return self._env_db

    def _pull(self, n: int) -> np.ndarray:
        out = np.zeros(n, np.float32)
        i = 0
        drained = False
        with self._lock:
            t_block = self._anchor + self._consumed / self.sr
            while i < n and self._buf:
                head = self._buf[0]
                k = min(n - i, len(head))
                out[i:i + k] = head[:k]
                if k == len(head):
                    self._buf.popleft()
                else:
                    self._buf[0] = head[k:]
                i += k
                self._queued -= k
                self._consumed += k
            if self._was_playing and self._queued <= 0:
                self._queued = 0
                self._was_playing = False
                drained = True
        if i:
            self.reference.write(t_block, out[:i], self.sr)
            self._report(t_block, out[:i])
        if drained:
            self._emit_position(t_block + i / self.sr, 0.0)
        db = rms_db(out) if i else -90.0
        self._env_db = db if db > self._env_db else self._env_db + (db - self._env_db) * 0.3
        if drained and self.on_drained:
            self.on_drained()
        return out

    def _report(self, t: float, x: np.ndarray) -> None:
        if self._win_n == 0:
            self._win_t = t
        self._win_sq += float(np.dot(x, x))
        self._win_n += len(x)
        if self._win_n >= self.sr * POSITION_MS // 1000:
            self._emit_position(self._win_t, float(np.sqrt(self._win_sq / self._win_n)))
            self._win_sq, self._win_n = 0.0, 0

    def _emit_position(self, t: float, rms: float) -> None:
        for fn in ([self.on_position] if self.on_position else []) + position_listeners:
            try:
                fn(t, rms)
            except Exception:
                log.exception("position listener failed")

    def _cb(self, outdata, frames, _t, _status) -> None:
        outdata[:, 0] = self._pull(frames)

    def _null_loop(self) -> None:
        step = 0.02
        while True:
            time.sleep(step)
            self._pull(int(self.sr * step))

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()


# ─────────────────────────────────────────────────────────────────────
#  Microphone
# ─────────────────────────────────────────────────────────────────────
class Frame(np.ndarray):
    """A mic frame that knows when its last sample was captured (`t`, monotonic seconds)."""

    t: float = 0.0

    def __array_finalize__(self, obj) -> None:
        self.t = getattr(obj, "t", 0.0)


class Microphone:
    """Delivers 20 ms 16 kHz float frames (`Frame`, stamped with their capture time) to `on_frame`
    on the PortAudio thread."""

    RESYNC_S = 0.05                    # re-anchor the sample clock when the device clock disagrees this much
    DRIFT_CHECK_S = 60.0               # without a device clock: compare the counter with time.monotonic() this often
    DRIFT_S = 0.02

    def __init__(self, on_frame: Callable[[np.ndarray], None], device=None) -> None:
        self.on_frame = on_frame
        self._bad_listeners: set = set()
        self._stream = None
        self._carry = np.zeros(0, np.float32)
        self._emitted = 0              # 16 kHz samples handed out as frames so far
        self._t0: float | None = None  # monotonic time of 16 kHz sample 0
        self._clock_off: float | None = None   # monotonic minus PortAudio stream time
        self._checked = 0.0            # monotonic time of the last counter-vs-clock check (no device clock)
        self._offsets: collections.deque = collections.deque(maxlen=25)   # recent clock-minus-counter readings
        self.error = _SD_ERR
        if sd is None:
            return
        for dev in ((device, None) if device is not None else (None,)):
            try:
                info = sd.query_devices(dev, "input")
                self.sr = int(info["default_samplerate"])
                self._stream = sd.InputStream(samplerate=self.sr, channels=1, dtype="float32",
                                              device=dev, blocksize=int(self.sr * FRAME_MS / 1000),
                                              callback=self._cb)
                self._stream.start()
                self.error = None
                log.info("mic: %s @ %d Hz", info["name"], self.sr)
                break
            except Exception as e:     # noqa: BLE001
                self.error = str(e)
                log.warning("microphone unavailable (%s): %s", dev, e)
                self._stream = None

    @property
    def available(self) -> bool:
        return self._stream is not None

    def _buffer_time(self, time_info, frames: int, status) -> float | None:
        """Monotonic capture time of the first sample of this buffer, from the device clock when it
        reports one, else None."""
        try:
            adc = float(time_info.inputBufferAdcTime)
            cur = float(time_info.currentTime)
        except (AttributeError, TypeError, ValueError):
            return None
        if adc <= 0.0 or cur <= 0.0 or adc > cur:
            return None
        if self._clock_off is None:
            self._clock_off = time.monotonic() - cur
        return adc + self._clock_off

    def _cb(self, indata, frames, time_info, status) -> None:
        x = resample(indata[:, 0], self.sr, MIC_SR)
        t_buf = self._buffer_time(time_info, frames, status)
        start = self._emitted + len(self._carry)           # index of x's first new sample
        if t_buf is None:                                  # no usable device clock: count samples
            now = time.monotonic()
            end = (start + len(x)) / MIC_SR
            if self._t0 is None or (status is not None and getattr(status, "input_overflow", False)):
                self._t0, self._checked = now - end, now
                self._offsets.clear()
            else:
                self._offsets.append(now - (self._t0 + end))
                if now - self._checked >= self.DRIFT_CHECK_S and len(self._offsets) == self._offsets.maxlen:
                    self._checked = now
                    drift = min(self._offsets)             # the least-delayed callback is closest to the truth
                    if abs(drift) > self.DRIFT_S:
                        self._t0 += drift
                        self._offsets.clear()
        elif self._t0 is None or abs(t_buf - (self._t0 + start / MIC_SR)) > self.RESYNC_S:
            self._t0 = t_buf - start / MIC_SR
        x = np.concatenate([self._carry, x])
        n = len(x) // FRAME
        for i in range(n):
            f = x[i * FRAME:(i + 1) * FRAME].copy().view(Frame)
            f.t = self._t0 + (self._emitted + FRAME) / MIC_SR
            self._emitted += FRAME
            self.on_frame(f)
            self._emit_frame(f)
        self._carry = x[n * FRAME:]

    def _emit_frame(self, f: np.ndarray) -> None:
        for fn in frame_listeners:
            try:
                fn(f)
            except Exception:
                if fn not in self._bad_listeners:
                    self._bad_listeners.add(fn)
                    log.exception("frame listener failed")

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()


def to_pcm16(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()


def wav_bytes(pcm16: bytes, sr: int = MIC_SR) -> bytes:
    import io
    import wave
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm16)
    return b.getvalue()


_SHARED_HOSTS = ("pipewire", "pulse", "default")


def _is_raw_hw(name: str) -> bool:
    return "hw:" in name.lower()


def resolve_device(spec: str, *, kind: str = "input"):
    """Map a configured name or index to something PortAudio accepts.

    With ``AUDIO_SHARED`` (default on) a PipeWire/pulse-backed device is preferred over the raw
    ``hw:`` entry so other apps can hold the same microphone at the same time.
    """
    if not spec:
        return None
    try:
        return int(spec)
    except ValueError:
        pass
    if sd is None:
        return spec
    try:
        devices = list(sd.query_devices())
    except Exception as e:             # noqa: BLE001
        log.warning("cannot list audio devices: %s", e)
        return spec
    from .config import Settings
    cfg = Settings.load()
    cfg.raw.setdefault("AUDIO_SHARED", os.environ.get("AUDIO_SHARED", ""))
    shared = cfg.flag("AUDIO_SHARED", True)
    key = f"max_{kind}_channels"
    want = spec.lower()
    usable = [(i, str(d["name"])) for i, d in enumerate(devices) if d.get(key, 0) > 0]
    named = [(i, n) for i, n in usable if want in n.lower()]
    if not shared:
        return named[0][0] if named else spec
    for i, n in named:
        if not _is_raw_hw(n):
            return i
    for host in _SHARED_HOSTS:
        for i, n in usable:
            if n.lower() == host:
                log.info("audio: %r not exposed by PipeWire; using the %s device (system default source)",
                         spec, n)
                return i
    return named[0][0] if named else spec
