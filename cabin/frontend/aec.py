"""Software acoustic echo cancellation for microphones without hardware AEC (WM8960 path).

``Aec`` is a partitioned frequency-domain adaptive filter (overlap-save, per-bin normalised LMS,
constrained gradient) fed with 20 ms mic frames and the aligned playback reference. ``AecStage``
runs it on the front-end worker, ``ResidualGateStage`` then keeps the VAD closed while Nova is
speaking unless the driver is clearly louder than the echo that is left, and an ``EchoMonitor``
falls back to half-duplex when the canceller cannot get the echo down.

Settings: ``AEC`` auto|on|off, ``AEC_DELAY_MS`` (fixed bulk delay), ``AEC_FALLBACK``
half_duplex|none.
"""
from __future__ import annotations

import logging
import math
import os
import subprocess
from collections import deque

import numpy as np

log = logging.getLogger("frontend.aec")

SR = 16000
FRAME = 320
HARDWARE_AEC_DEVICES = ("xvf3800", "respeaker")
PLAYBACK_ACTIVE_DB = -60.0       # Player envelope above this = Nova is speaking
REF_ACTIVE_DB = -62.0            # reference frame level above this = the filter has something to learn


def _db(power: float) -> float:
    return 10.0 * math.log10(power + 1e-12)


def _rms_db(x: np.ndarray) -> float:
    return _db(float(np.mean(np.square(x, dtype=np.float64))))


# ─────────────────────────────────────────────────────────────────────
#  Adaptive filter
# ─────────────────────────────────────────────────────────────────────
class Aec:
    """Block NLMS echo canceller: ``process(mic, ref)`` returns ``mic`` minus the estimated echo.

    ``mic`` and ``ref`` are frames of ``frame`` samples captured and played at the same time. The
    filter spans ``tail_ms``; a bulk delay (``set_delay``) shifts that window when the echo arrives
    later than the tail. Adaptation slows down by itself while something other than the echo
    (the driver) is in the microphone signal.
    """

    impl = "numpy-fdaf"
    MAX_DELAY_MS = 1000
    LEAK = 1e-4                  # per adapted frame; keeps unobservable taps from drifting
    PERSIST_FRAMES = 125         # an error jump that lasts this long is a new echo path, not the driver

    def __init__(self, sr: int = SR, frame: int = FRAME, tail_ms: int = 256, mu: float = 1.0,
                 delay_ms: float = 0.0) -> None:
        self.sr, self.B, self.mu = sr, frame, mu
        self.N = 2 * frame
        self.nb = frame + 1
        self.P = max(1, math.ceil(tail_ms * sr / 1000 / frame))
        self._max_delay = int(self.MAX_DELAY_MS * sr / 1000)
        self._line = np.zeros(self._max_delay + 2 * frame, np.float32)
        self._fdl = np.zeros((2 * self.P, self.nb), np.complex64)
        self._pos = 0
        self.W = np.zeros((self.P, self.nb), np.complex64)
        self._pxs: deque[float] = deque(maxlen=self.P)
        self._r_base = 0.0
        self._dt_run = 0
        self.hold_baseline = False   # set while the driver is known to be talking: never re-seed then
        self._pm_s = self._pe_s = 0.0
        self.delay = 0
        self.set_delay(int(delay_ms * sr / 1000))

    @property
    def tail_ms(self) -> float:
        return self.P * self.B * 1000.0 / self.sr

    @property
    def erle_db(self) -> float | None:
        """Mic-to-output power ratio over recent frames with playback, in dB."""
        if self._pe_s <= 0.0:
            return None
        return _db(self._pm_s) - _db(self._pe_s)

    def set_delay(self, samples: int) -> None:
        samples = int(min(max(samples, 0), self._max_delay))
        if samples != self.delay:
            self.delay = samples
            self.reset()

    def reset(self) -> None:
        """Forget the learned echo path and the reference history."""
        self.W[:] = 0
        self.clear_history()
        self.reset_baseline()
        self._pm_s = self._pe_s = 0.0

    def reset_baseline(self) -> None:
        """Forget what a well-cancelled frame looks like (after a delay change or a clock jump)."""
        self._r_base = 0.0
        self._dt_run = 0

    def aligned_power(self) -> float:
        """Mean power of the reference that the echo at the mic right now is made of: the delayed
        reference blocks of the filter window, weighted by how much of the learned echo path sits
        at each lag."""
        if not self._pxs:
            return 0.0
        px = np.fromiter(reversed(self._pxs), np.float64, len(self._pxs))      # lag 0, 1, ... blocks
        w = np.einsum("pf,pf->p", self.W.real, self.W.real) + np.einsum("pf,pf->p", self.W.imag, self.W.imag)
        w = w[:len(px)].astype(np.float64)
        tot = float(w.sum())
        if tot < 1e-12:
            return float(px.mean())
        return float(np.dot(w, px) / tot)

    def clear_history(self) -> None:
        self._line[:] = 0
        self._fdl[:] = 0
        self._pxs.clear()

    def process(self, mic: np.ndarray, ref: np.ndarray) -> np.ndarray:
        B, N, P = self.B, self.N, self.P
        mic = np.nan_to_num(np.asarray(mic, np.float32), nan=0.0, posinf=0.0, neginf=0.0)
        ref = np.nan_to_num(np.asarray(ref, np.float32), nan=0.0, posinf=0.0, neginf=0.0)
        line = self._line
        line[:-B] = line[B:]
        line[-B:] = ref
        L = len(line)
        xw = line[L - N - self.delay:L - self.delay]
        X = np.fft.rfft(xw)
        self._pos = (self._pos - 1) % P
        self._fdl[self._pos] = X
        self._fdl[self._pos + P] = X
        view = self._fdl[self._pos:self._pos + P]

        y = np.fft.irfft(np.einsum("pf,pf->f", self.W, view), n=N)[B:].astype(np.float32)
        e = mic - y
        if not np.isfinite(e).all():             # the filter blew up: start over from a clean state
            log.warning("echo canceller diverged; resetting")
            self.reset()
            return mic.copy()

        px = float(np.dot(xw[B:], xw[B:])) / B
        self._pxs.append(px)
        span = sum(self._pxs) / len(self._pxs)
        if span < 1e-9:                          # nothing played in the filter's window
            return e
        pe = float(np.dot(e, e)) / B
        pm = float(np.dot(mic, mic)) / B
        self._pm_s += 0.05 * (pm - self._pm_s)
        self._pe_s += 0.05 * (pe - self._pe_s)

        # something other than the echo in the mic: the error stays close to the mic signal
        r = pe / (pm + 1e-12)
        if self._r_base <= 0.0:
            self._r_base = r
        elif r < self._r_base:
            self._r_base += 0.3 * (r - self._r_base)
        else:
            self._r_base += 0.0004 * (r - self._r_base)
        if r > 3.0 * self._r_base:
            self._dt_run += 0 if self.hold_baseline else 1
            if self._dt_run >= self.PERSIST_FRAMES:   # too long to be speech over the echo: the path changed
                self._r_base, self._dt_run = r, 0
        else:
            self._dt_run = 0
        gain = min(1.0, max(0.05, 3.0 * self._r_base / (r + 1e-20))) ** 2

        if px < 1e-9:
            return e
        pw = (view.real * view.real + view.imag * view.imag).sum(axis=0)
        E = np.fft.rfft(np.concatenate([np.zeros(B, np.float32), e]))
        # the error spectrum in the denominator bounds a step taken on a reference far weaker than
        # the error (a reference pause, or history just cleared) instead of letting it blow up
        G = np.conj(view) * (E / (pw + (E.real * E.real + E.imag * E.imag) + (1e-4 * float(pw.max()) + 1e-10)))
        g = np.fft.irfft(G, n=N, axis=1)
        g[:, B:] = 0.0
        self.W *= 1.0 - self.LEAK
        self.W += (self.mu * gain) * np.fft.rfft(g, axis=1).astype(np.complex64)
        return e


# ─────────────────────────────────────────────────────────────────────
#  Delay estimation
# ─────────────────────────────────────────────────────────────────────
def calibration_chirp(sr: int = SR, ms: int = 300, f0: float = 300.0, f1: float = 3800.0,
                      level: float = 0.5) -> np.ndarray:
    """Linear sweep with raised-cosine edges, the probe played once at start-up."""
    n = int(sr * ms / 1000)
    t = np.arange(n) / sr
    k = (f1 - f0) / (n / sr)
    x = np.sin(2 * np.pi * (f0 * t + 0.5 * k * t * t))
    w = np.hanning(n) ** 0.5
    return (level * w * x).astype(np.float32)


def estimate_delay_ex(mic: np.ndarray, ref: np.ndarray, sr: int = SR,
                      max_delay_ms: float = 1000.0, min_delay_ms: float = 0.0) -> tuple[int, float]:
    """Lag in samples by which ``mic`` trails ``ref`` and a peak-to-background score.

    Only lags between ``min_delay_ms`` and ``max_delay_ms`` are considered.

    Phase-transform weighted cross-correlation over the speech band, so it locks onto a chirp as
    well as onto ordinary playback.
    """
    n = min(len(mic), len(ref))
    if n < 64:
        return 0, 0.0
    mic = np.asarray(mic[:n], np.float32)
    ref = np.asarray(ref[:n], np.float32)
    nfft = 1 << (2 * n - 1).bit_length()
    C = np.fft.rfft(mic, nfft) * np.conj(np.fft.rfft(ref, nfft))
    f = np.fft.rfftfreq(nfft, 1.0 / sr)
    band = (f >= 150.0) & (f <= min(4000.0, sr / 2.0))
    mag = np.abs(C)
    C = np.where(band, C / (mag + 0.05 * float(mag[band].mean()) + 1e-12), 0)
    cc = np.fft.irfft(C, nfft)
    maxlag = min(int(max_delay_ms * sr / 1000), n - 1)
    minlag = min(max(int(min_delay_ms * sr / 1000), 0), maxlag)
    seg = np.abs(cc[minlag:maxlag + 1])
    k = int(np.argmax(seg))
    return minlag + k, float(seg[k] / (np.median(seg) + 1e-12))


def estimate_delay(mic: np.ndarray, ref: np.ndarray, sr: int = SR, max_delay_ms: float = 1000.0) -> int:
    return estimate_delay_ex(mic, ref, sr, max_delay_ms)[0]


# ─────────────────────────────────────────────────────────────────────
#  Residual gate
# ─────────────────────────────────────────────────────────────────────
class ResidualGate:
    """Decides whether a frame after echo cancellation may reach the VAD.

    While Nova is speaking the gate stays closed. It models the residual it expects from the
    echo, ``noise + k * playback power`` (both learned while the gate is closed), and opens once the
    residual stays ``open_db`` above that for ``hold_ms``: somebody is talking over Nova. Without
    playback it is always open. ``jitter_db`` is the allowance for frame-to-frame level noise.
    What it learned is kept across pauses shorter than ``keep_ms`` (sentences of one reply).
    """

    WARMUP_MS = 400

    def __init__(self, open_db: float = 12.0, hold_ms: int = 300, release_ms: int = 400,
                 frame_ms: int = 20, jitter_db: float = 1.0, keep_ms: int = 1500,
                 fast_db: float = 20.0, fast_ms: int = 120) -> None:
        self.open_db = open_db
        self.fast_db = fast_db                   # a residual this far over the model counts for ...
        self.fast_weight = max(1.0, hold_ms / fast_ms)   # ... this many ordinary frames
        self.jitter_db = jitter_db
        self.hold = max(1, round(hold_ms / frame_ms))
        self.release = max(1, round(release_ms / frame_ms))
        self.warmup = round(self.WARMUP_MS / frame_ms)
        self.keep = max(1, round(keep_ms / frame_ms))
        self.half_duplex = False
        self.noise_p: float | None = None
        self.k = 0.0
        self.is_open = False
        self._run = 0.0
        self._quiet = 0
        self._seen = 0
        self._gap = 0

    def reset(self) -> None:
        self.is_open = False
        self._run = 0.0
        self._quiet = 0

    def _forget(self) -> None:
        self.reset()
        self.noise_p, self.k = None, 0.0
        self._seen = self._gap = 0

    def relearn(self) -> None:
        """The echo canceller started over, so the residual will jump: learn the echo again."""
        self._forget()
        self._gap = 0

    def expected_db(self, playback_db: float | None) -> float | None:
        if self.noise_p is None:
            return None
        pp = 0.0 if playback_db is None else 10 ** (max(playback_db, -90.0) / 10)
        return _db(self.noise_p + self.k * pp)

    def _learn(self, p: float, pp: float, fast: bool) -> None:
        if p < self.noise_p:
            self.noise_p += 0.15 * (p - self.noise_p)
        else:
            self.noise_p += (0.3 if fast else 0.002) * (p - self.noise_p)
        if pp > 1e-9:
            obs = min(max(p - self.noise_p, 0.0) / pp, 10.0)
            self.k += (0.3 if fast else 0.05) * (obs - self.k)

    def allow(self, residual_db: float, playback_db: float | None, speaking: bool | None = None) -> bool:
        """``playback_db`` is the level of the reference (for the echo model); ``speaking`` says whether
        Nova is audible at the mic, by default whether ``playback_db`` is above -60 dB."""
        if speaking is None:
            speaking = playback_db is not None and playback_db > PLAYBACK_ACTIVE_DB
        if not speaking:
            self.reset()
            self._gap += 1
            if self._gap > self.keep:
                self._forget()
            return True
        self._gap = 0
        self._seen += 1
        if self.half_duplex:
            return False
        p = 10 ** (residual_db / 10)
        pp = 0.0 if playback_db is None else 10 ** (max(playback_db, -90.0) / 10)
        if self.noise_p is None:
            self.noise_p = p
        if self._seen <= self.warmup:            # the echo has not reached the mic yet; learn it
            self._learn(p, pp, fast=True)
            return False
        expected = _db(self.noise_p + self.k * pp)
        limit = expected + self.open_db - self.jitter_db
        if self.is_open:
            if residual_db >= limit - 6.0:
                self._quiet = 0
            else:
                self._quiet += 1
                if self._quiet >= self.release:
                    self.reset()
            return self.is_open
        if residual_db >= limit:
            self._run += self.fast_weight if residual_db >= expected + self.fast_db else 1.0
        else:
            self._run = max(0.0, self._run - 1.0)
            if self._run == 0.0 and residual_db < expected + 0.5 * self.open_db:
                self._learn(p, pp, fast=False)     # never while an onset is pending: it may be the driver
        if self._run >= self.hold:
            self.is_open = True
            self._quiet = 0
        return self.is_open


# ─────────────────────────────────────────────────────────────────────
#  Device policy
# ─────────────────────────────────────────────────────────────────────
_hw_cache: dict[str, bool] = {}


def _default_source_name() -> str:
    try:
        out = subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True, timeout=1.5,
                             check=False)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:                                   # noqa: BLE001
        return ""


def hardware_aec_present(cfg) -> bool:
    """True when the configured microphone is a device with its own echo canceller."""
    spec = cfg.get("AUDIO_INPUT_DEVICE", "")
    if spec in _hw_cache:
        return _hw_cache[spec]
    names = [spec]
    try:
        from .. import audio
        if audio.sd is not None:
            dev = audio.resolve_device(spec)
            info = audio.sd.query_devices(dev, "input")
            names.append(str(info["name"]))
            if str(info["name"]).lower() in audio._SHARED_HOSTS:
                names.append(_default_source_name())
    except Exception as e:                              # noqa: BLE001
        log.debug("input device lookup failed: %s", e)
    hit = any(k in n.lower() for n in names for k in HARDWARE_AEC_DEVICES)
    _hw_cache[spec] = hit
    return hit


def echo_mode(cfg) -> str:
    """Active echo-cancellation mode: ``on`` (software), ``off`` or ``hardware``."""
    want = cfg.get("AEC", "auto").strip().lower()
    if want in ("off", "false", "0", "no"):
        return "off"
    if want in ("on", "true", "1", "yes"):
        return "on"
    return "hardware" if hardware_aec_present(cfg) else "on"


# ─────────────────────────────────────────────────────────────────────
#  Front-end stages
# ─────────────────────────────────────────────────────────────────────
class AecStage:
    """Subtracts Nova's own playback from the mic frame and watches how well that works.

    ``speaking`` says whether Nova's reference was audible within the filter tail, ``ref_db`` is the
    level of the current reference frame; the residual gate uses both.
    """

    WINDOW_FRAMES = 50              # one second of playback per measurement window
    BAD_WINDOWS = 5                 # consecutive bad windows before half-duplex
    GOOD_WINDOWS = 10               # consecutive good windows before leaving it
    GOOD_ERLE_DB = 10.0             # a window is good with this much cancellation ...
    NEAR_FLOOR_DB = 6.0             # ... or a residual this close to the idle noise floor
    CALIBRATE_AFTER = 75            # playback frames before the first delay estimate
    CENTER_MS = 80                  # where the echo should sit inside the filter tail
    LEAD = 320                      # samples the reference is read ahead of the mic stamp
    SEARCH_MS = 200                 # first delay search: this far around the player's latency

    def __init__(self, cfg, get_reference, gate: ResidualGate | None = None, sink=None) -> None:
        self.cfg = cfg
        self._sink = sink
        self._get_reference = get_reference
        self.gate = gate
        fixed = cfg.num("AEC_DELAY_MS", 0.0) if cfg.get("AEC_DELAY_MS") else None
        self.fixed_delay = fixed is not None
        self.aec = Aec(delay_ms=fixed or 0.0)
        self.fallback = cfg.get("AEC_FALLBACK", "half_duplex").strip().lower() == "half_duplex"
        self.half_duplex = False
        self.has_reference = False
        self.speaking = False
        self.ref_db = -90.0
        self._end: int | None = None
        self._idle = 10 ** 6
        self._active_frames = 0
        self._calibrated = self.fixed_delay
        self._retried = False
        self._hist: deque = deque(maxlen=100)
        self._noise_p: float | None = None
        self._w_mic = self._w_err = 0.0
        self._wn = 0
        self._windows: list[float] = []
        self._good: list[bool] = []

    def reset(self) -> None:
        """Turn boundary: the echo path is unchanged, so the filter and the fallback decision stay."""

    def _ref_frame(self, tap, n: int, t: float) -> np.ndarray:
        idx = round(t * tap.sr) + self.LEAD
        if self._end is None or abs(idx - (self._end + n)) > 640:
            if self._end is not None:               # the clock jumped: what was learned about timing is stale
                self.aec.clear_history()
                self.aec.reset_baseline()
                self._hist.clear()
            end = idx
        else:
            end = self._end + n
            off = idx - end
            if abs(off) > 16:
                end += 1 if off > 0 else -1
        self._end = end
        return tap.read_at(end, n)

    def process(self, frame: np.ndarray, ctx) -> np.ndarray | None:
        out = self._process(frame, ctx)
        if self._sink is not None:
            self._sink(out)
        return out

    def _process(self, frame: np.ndarray, ctx) -> np.ndarray:
        tap = self._get_reference()
        stats = ctx.stats
        self.has_reference = tap is not None
        if tap is None:
            self.speaking = False
            return frame
        n = len(frame)
        ref = self._ref_frame(tap, n, ctx.t)
        raw_db = _rms_db(ref)
        self.ref_db = raw_db
        active = raw_db > REF_ACTIVE_DB
        if active:
            self._idle = 0
            self._active_frames += 1
        else:
            self._idle += 1
        tail_frames = self.aec.P + self.aec.delay // n + 2
        self.speaking = self._idle <= tail_frames
        if not self.speaking:
            if self._idle == tail_frames + 1:
                self.aec.clear_history()
            self._track_noise(frame, stats)
            return frame
        self._hist.append((np.asarray(frame, np.float32).copy(), ref))
        self.aec.hold_baseline = self.gate is not None and self.gate.is_open
        out = self.aec.process(frame, ref)
        self.ref_db = _db(self.aec.aligned_power())
        stats["aec_delay_ms"] = round(self.aec.delay * 1000 / self.aec.sr)
        erle = self.aec.erle_db
        stats["erle_db"] = None if erle is None else round(erle, 1)
        if active:
            self._measure(frame, out, stats)
        return out

    def _track_noise(self, frame: np.ndarray, stats: dict) -> None:
        """Mic power with nothing playing: what a clean residual should look like."""
        p = float(np.dot(frame, frame)) / len(frame)
        if self._noise_p is None or p < self._noise_p:
            self._noise_p = p if self._noise_p is None else self._noise_p + 0.1 * (p - self._noise_p)
        else:
            self._noise_p += 0.002 * (p - self._noise_p)
        stats["noise_floor_db"] = round(_db(self._noise_p), 1)

    def _measure(self, mic: np.ndarray, out: np.ndarray, stats: dict) -> None:
        if not self._calibrated and self._active_frames >= self.CALIBRATE_AFTER:
            self._calibrate(stats)
        if self.gate is not None and self.gate.is_open:        # the driver is talking: not an echo measurement
            return
        self._w_mic += float(np.dot(mic, mic))
        self._w_err += float(np.dot(out, out))
        self._wn += 1
        if self._wn < self.WINDOW_FRAMES:
            return
        erle = _db(self._w_mic) - _db(self._w_err)
        excess = _db(self._w_err / self._wn / len(out)) - _db(self._noise_p) if self._noise_p else 99.0
        self._w_mic = self._w_err = 0.0
        self._wn = 0
        stats["window_erle_db"] = round(erle, 1)
        self._good.append(erle >= self.GOOD_ERLE_DB or excess <= self.NEAR_FLOOR_DB)
        del self._good[:-self.GOOD_WINDOWS]
        if self.half_duplex:
            if len(self._good) >= self.GOOD_WINDOWS and all(self._good):
                self._set_half_duplex(False, stats)
            return
        recent = self._good[-self.BAD_WINDOWS:]
        if len(recent) < self.BAD_WINDOWS or any(recent):
            return
        if not self._retried and not self.fixed_delay:
            self._retried = True
            self._good.clear()
            self._calibrate(stats)
            return
        if self.fallback:
            self._set_half_duplex(True, stats)
            log.warning("echo not cancelled (window ERLE %.1f dB): mic closed while Nova speaks", erle)

    def _set_half_duplex(self, on: bool, stats: dict) -> None:
        self.half_duplex = on
        self._good.clear()
        if self.gate is not None:
            self.gate.half_duplex = on
            self.gate.reset()
        stats["mode"] = "half_duplex" if on else "full_duplex"
        if not on:
            self._retried = False
            log.info("echo cancelled again: full duplex")

    def _seed_ms(self, tap) -> float | None:
        fn = getattr(tap, "delay_fn", None)
        try:
            return None if fn is None else float(fn()) * 1000.0
        except Exception:                                   # noqa: BLE001
            return None

    def _calibrate(self, stats: dict) -> None:
        self._calibrated = True
        if len(self._hist) < 25:
            return
        mic = np.concatenate([m for m, _ in self._hist])
        ref = np.concatenate([r for _, r in self._hist])
        sr, top = self.aec.sr, self.aec.MAX_DELAY_MS
        lag, score = 0, 0.0
        seed = self._seed_ms(self._get_reference())
        if seed is not None:                                # first look near the player's own latency
            centre = seed + self.LEAD * 1000.0 / sr
            lag, score = estimate_delay_ex(mic, ref, sr, centre + self.SEARCH_MS,
                                           max(0.0, centre - self.SEARCH_MS))
        if score < 6.0:
            lag, score = estimate_delay_ex(mic, ref, sr, top)
        stats["aec_delay_score"] = round(score, 1)
        if score < 6.0:
            return
        target = max(0, lag - int(self.CENTER_MS * sr / 1000))
        if abs(target - self.aec.delay) >= int(0.030 * sr):
            log.info("echo delay %.0f ms: bulk delay set to %.0f ms", lag * 1000 / sr, target * 1000 / sr)
            self.aec.set_delay(target)
            if self.gate is not None:
                self.gate.relearn()
            self._good.clear()
            self._w_mic = self._w_err = 0.0
            self._wn = 0


class ResidualGateStage:
    """Drops frames while Nova speaks unless the residual says the driver is talking over her.

    A frame that passes is judged by the gate alone: the VAD behind it gets no playback level, so
    its own echo threshold (which would learn from the driver's voice) stays out of the way.
    The last ``REPLAY`` frames held back are fed to the VAD the moment the gate opens, so the
    driver's first syllable is not lost to the gate's hold time.
    """

    REPLAY = 23                     # about the gate's hold plus the VAD's start time, in frames

    def __init__(self, gate: ResidualGate, aec: AecStage | None = None, on_close=None, in_speech=None,
                 vad=None) -> None:
        self.gate = gate
        self._aec = aec
        self._on_close = on_close
        self._in_speech = in_speech
        self._vad = vad
        self._ring: deque = deque(maxlen=self.REPLAY)
        self._was_open = True

    def reset(self) -> None:
        self.gate.reset()
        self._was_open = True
        self._ring.clear()

    def process(self, frame: np.ndarray, ctx) -> np.ndarray | None:
        aec = self._aec
        if aec is not None and aec.has_reference:
            ok = self.gate.allow(_rms_db(frame), aec.ref_db, aec.speaking)
        else:
            ok = self.gate.allow(_rms_db(frame), ctx.playback_db)
        ctx.stats["gate_open"] = ok
        ctx.playback_db = None
        if ok:
            if not self._was_open and self.gate.is_open and self._vad is not None:
                for old in self._ring:                       # the driver's onset, held back until now
                    self._vad.process(old, ctx)
            self._ring.clear()
            self._was_open = True
            return frame
        busy = self._in_speech is not None and self._in_speech()
        if self._was_open:
            self._was_open = False
            if self._on_close is not None and not busy:      # a half-heard start is stale; a running turn is not
                self._on_close()
        if busy:                                             # a running turn must still see its end
            self._ring.clear()                               # what it sees now must not be replayed later
            return frame
        self._ring.append(frame)
        return None


def install(front_end) -> None:
    """Add the echo stages in front of the VAD stage of ``front_end`` and publish their state.

    Front ends without a VAD stage (custom stage lists) are left alone.
    """
    from .. import audio
    cfg = front_end.cfg
    stats = front_end.stats
    mode = echo_mode(cfg)
    stats["aec"] = mode
    stats["mode"] = "full_duplex"
    if mode != "on":
        return
    stages = front_end.stages
    at = next((i for i, s in enumerate(stages) if isinstance(s, audio.VadStage)), None)
    if at is None:
        return
    gate = ResidualGate()

    def reference():
        return front_end.reference if front_end.reference is not None else audio.current_reference

    aec = AecStage(cfg, reference, gate, front_end.clean_frames.append)
    vad = stages[at]
    owner = getattr(getattr(vad, "_detect", None), "__self__", None)
    stats.update(aec_impl=aec.aec.impl, aec_delay_ms=round(aec.aec.delay * 1000 / aec.aec.sr),
                 erle_db=None)
    in_speech = (lambda: bool(getattr(owner, "in_speech", False))) if owner is not None else None
    stages[at:at] = [aec, ResidualGateStage(gate, aec, getattr(vad, "reset", None), in_speech, vad)]
    if os.environ.get("AEC_DEBUG"):
        log.info("software AEC on: %s, tail %.0f ms", aec.aec.impl, aec.aec.tail_ms)
