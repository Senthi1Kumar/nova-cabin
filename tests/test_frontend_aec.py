"""Software echo cancellation, delay estimation, residual gate and the half-duplex fallback."""
import time

import numpy as np
import pytest

from cabin import audio
from cabin.audio import VadStage, rms_db
from cabin.config import Settings
from cabin.frontend import FrontEnd
from cabin.frontend.aec import (
    Aec,
    AecStage,
    ResidualGate,
    ResidualGateStage,
    calibration_chirp,
    estimate_delay,
)

SR = 16000
B = 320


def speechlike(n: int, seed: int, rms: float = 0.1) -> np.ndarray:
    """Coloured noise with an irregular syllable-rate envelope."""
    rng = np.random.default_rng(seed)
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / SR)
    spec *= 1.0 / (1.0 + (f / 900.0) ** 2) + 0.6 / (1.0 + ((f - 2400.0) / 500.0) ** 2)
    x = np.fft.irfft(spec, n)
    t = np.arange(n) / SR
    env = 0.55 + 0.45 * np.sin(2 * np.pi * 3.1 * t + 0.4) * np.sin(2 * np.pi * 0.9 * t + 1.1)
    x *= np.clip(env, 0.05, 1.0)
    return (x * rms / np.sqrt(np.mean(x ** 2))).astype(np.float32)


def voiced(n: int, rms: float) -> np.ndarray:
    t = np.arange(n) / SR
    x = sum(np.sin(2 * np.pi * 140 * k * t + k) / k for k in range(1, 12))
    return (x * rms / np.sqrt(np.mean(x ** 2))).astype(np.float32)


def echo_of(ref: np.ndarray, delay_ms: float, gain_db: float, rir: bool = False) -> np.ndarray:
    d = int(delay_ms * SR / 1000)
    g = 10 ** (gain_db / 20)
    h = np.zeros(d + 600, np.float32)
    h[d] = g
    if rir:
        h[d + 80] = -0.35 * g
        h[d + 190] = 0.22 * g
        h[d + 420] = -0.10 * g
    return np.convolve(ref, h)[:len(ref)].astype(np.float32)


def run_aec(aec: Aec, mic: np.ndarray, ref: np.ndarray) -> np.ndarray:
    out = np.zeros_like(mic)
    for i in range(len(mic) // B):
        s = slice(i * B, (i + 1) * B)
        out[s] = aec.process(mic[s], ref[s])
    return out


def erle_db(echo: np.ndarray, out: np.ndarray, noise: np.ndarray, a: int, b: int) -> float:
    resid = out[a:b] - noise[a:b]
    return 10 * np.log10(np.mean(echo[a:b] ** 2) / (np.mean(resid ** 2) + 1e-20))


@pytest.mark.parametrize("delay_ms,gain_db,rir", [(80, -10, False), (0, -6, False), (200, -30, False),
                                                  (60, -12, True)])
def test_aec_removes_synthetic_echo(delay_ms, gain_db, rir):
    n = 4 * SR
    ref = speechlike(n, 1)
    echo = echo_of(ref, delay_ms, gain_db, rir)
    noise = (np.random.default_rng(2).standard_normal(n) * 1e-4).astype(np.float32)
    out = run_aec(Aec(), echo + noise, ref)
    assert erle_db(echo, out, noise, 2 * SR, 4 * SR) >= 18.0


def test_aec_idle_reference_is_transparent():
    aec = Aec()
    x = speechlike(B, 3)
    assert np.allclose(aec.process(x, np.zeros(B, np.float32)), x)


def test_aec_survives_double_talk():
    n = 8 * SR
    ref = speechlike(n, 4)
    echo = echo_of(ref, 80, -10)
    noise = (np.random.default_rng(5).standard_normal(n) * 1e-4).astype(np.float32)
    driver = np.zeros(n, np.float32)
    driver[3 * SR:5 * SR] = voiced(2 * SR, 0.1)
    out = run_aec(Aec(), echo + noise + driver, ref)
    assert erle_db(echo, out, noise, 7 * SR, 8 * SR) >= 18.0
    assert rms_db(out[4 * SR:5 * SR]) > rms_db(driver[4 * SR:5 * SR]) - 6.0     # driver voice kept


def test_aec_frame_time_budget():
    aec = Aec()
    ref = speechlike(SR * 2, 6)
    mic = echo_of(ref, 80, -10)
    run_aec(aec, mic[:SR], ref[:SR])
    t = time.perf_counter()
    run_aec(aec, mic[SR:], ref[SR:])
    per_frame = (time.perf_counter() - t) / (SR // B)
    assert per_frame < 0.003


def _env(db: float, prev: float) -> float:
    return db if db > prev else prev + (db - prev) * 0.3


def test_aec_residual_gate_blocks_echo_and_passes_driver():
    n = 6 * SR
    ref = speechlike(n, 7)
    echo = echo_of(ref, 80, -10)
    noise = (np.random.default_rng(8).standard_normal(n) * 1e-4).astype(np.float32)
    aec, gate = Aec(), ResidualGate(open_db=12.0, hold_ms=300)
    env = -90.0
    allowed, resid_db = [], []
    for i in range(n // B):
        s = slice(i * B, (i + 1) * B)
        out = aec.process((echo + noise)[s], ref[s])
        env = _env(rms_db(ref[s]), env)
        r = rms_db(out)
        resid_db.append(r)
        allowed.append(gate.allow(r, env))
        if i == 4 * SR // B - 1:
            break
    assert not any(allowed)

    # the driver talks over Nova: +12 dB over the residual, for the next 400 ms
    floor_db = float(np.mean(resid_db[-50:]))
    drv = voiced(4 * SR, 10 ** (floor_db / 20) * 10 ** (12 / 20))
    opened_at = None
    start = 4 * SR // B
    for k in range(20):
        i = start + k
        s = slice(i * B, (i + 1) * B)
        out = aec.process((echo + noise)[s] + drv[k * B:(k + 1) * B], ref[s])
        env = _env(rms_db(ref[s]), env)
        if gate.allow(rms_db(out), env) and opened_at is None:
            opened_at = (k + 1) * 20
    assert opened_at is not None and opened_at <= 320


def test_gate_is_open_without_playback():
    g = ResidualGate()
    assert all(g.allow(-40.0, None) for _ in range(5))
    assert all(g.allow(-40.0, -80.0) for _ in range(5))


def test_gate_half_duplex_never_opens_during_playback():
    g = ResidualGate()
    g.half_duplex = True
    assert not any(g.allow(-10.0, -20.0) for _ in range(60))
    assert g.allow(-10.0, None)


def test_estimate_delay_chirp():
    chirp = calibration_chirp()
    assert len(chirp) == int(0.3 * SR)
    ref = np.concatenate([np.zeros(SR // 4, np.float32), chirp, np.zeros(SR // 2, np.float32)])
    d = int(0.120 * SR)
    mic = np.concatenate([np.zeros(d, np.float32), 0.3 * ref])[:len(ref)]
    mic = mic + np.random.default_rng(9).standard_normal(len(ref)).astype(np.float32) * 5e-3
    assert abs(estimate_delay(mic, ref) - d) <= int(0.010 * SR)


def test_estimate_delay_on_speech():
    ref = speechlike(2 * SR, 10)
    d = int(0.230 * SR)
    mic = echo_of(ref, 230, -8)
    assert abs(estimate_delay(mic, ref) - d) <= int(0.010 * SR)


# ── front-end wiring ────────────────────────────────────────────────────────────────

T0 = 1000.0


def _vad_stage(seen):
    def detect(frame, playback_db):
        seen.append(frame)
        return None, 0.0
    return VadStage(detect)


def _frontend(settings, tap, seen):
    return FrontEnd(Settings(settings), lambda e: None, reference=tap, stages=[_vad_stage(seen)])


def _drive(fe, tap, mic, ref, a=0, b=None, stamp_shift=0.0, ahead=2):
    """Feed frames a..b; the tap is written `ahead` frames early, as the player does."""
    env = -90.0
    total = len(mic) // B
    for i in range(a, b or total):
        for j in range(i if i == a else i + ahead, min(i + ahead + 1, total)):
            tap.write(T0 + j * 0.02, ref[j * B:(j + 1) * B], SR)
        s = slice(i * B, (i + 1) * B)
        env = _env(rms_db(ref[s]), env)
        fe.process(mic[s], env, T0 + (i + 1) * 0.02 + stamp_shift)


def test_frontend_stage_order_and_stats(monkeypatch):
    monkeypatch.setattr("cabin.frontend.aec.hardware_aec_present", lambda cfg: False)
    fe = _frontend({"AEC": "auto"}, audio.ReferenceTap(), [])
    assert [type(s).__name__ for s in fe.stages] == ["AecStage", "ResidualGateStage", "VadStage"]
    assert fe.stats["aec"] == "on" and fe.stats["aec_impl"] == "numpy-fdaf" and fe.mode == "full_duplex"


def test_frontend_aec_off_and_hardware(monkeypatch):
    monkeypatch.setattr("cabin.frontend.aec.hardware_aec_present", lambda cfg: False)
    fe = _frontend({"AEC": "off"}, audio.ReferenceTap(), [])
    assert [type(s).__name__ for s in fe.stages] == ["VadStage"] and fe.stats["aec"] == "off"
    monkeypatch.setattr("cabin.frontend.aec.hardware_aec_present", lambda cfg: True)
    fe = _frontend({"AEC": "auto"}, audio.ReferenceTap(), [])
    assert [type(s).__name__ for s in fe.stages] == ["VadStage"] and fe.stats["aec"] == "hardware"
    fe = _frontend({"AEC": "on"}, audio.ReferenceTap(), [])
    assert fe.stats["aec"] == "on"


def test_frontend_custom_stages_untouched():
    class Pass:
        def process(self, frame, ctx):
            return frame

    fe = FrontEnd(Settings({"AEC": "on"}), lambda e: None, stages=[Pass()])
    assert len(fe.stages) == 1


def test_frontend_cancels_echo_and_vad_sees_clean_audio(monkeypatch):
    monkeypatch.setattr("cabin.frontend.aec.hardware_aec_present", lambda cfg: False)
    n = 6 * SR
    ref = speechlike(n, 11)
    echo = echo_of(ref, 80, -10)
    noise = (np.random.default_rng(12).standard_normal(n) * 1e-4).astype(np.float32)
    seen: list = []
    tap = audio.ReferenceTap()
    fe = _frontend({"AEC": "on"}, tap, seen)
    _drive(fe, tap, echo + noise, ref)
    assert fe.mode == "full_duplex"
    assert fe.stats["erle_db"] is not None and fe.stats["erle_db"] > 15.0
    assert len(seen) < n // B                                 # the gate held the VAD back while Nova talked
    assert len(fe.clean_frames) == 64 and fe.clean_frames[-1].shape == (B,)


def test_half_duplex_fallback_engages(monkeypatch):
    monkeypatch.setattr("cabin.frontend.aec.hardware_aec_present", lambda cfg: False)
    n = 16 * SR
    ref = speechlike(n, 13)
    other = speechlike(n, 14)                    # not derived from the reference: cannot be cancelled
    seen: list = []
    tap = audio.ReferenceTap()
    fe = _frontend({"AEC": "on", "AEC_FALLBACK": "half_duplex"}, tap, seen)
    _drive(fe, tap, other, ref, 0, 14 * SR // B)
    assert fe.mode == "half_duplex" and fe.stats["mode"] == "half_duplex"
    seen.clear()
    _drive(fe, tap, other, ref, 14 * SR // B, 16 * SR // B)
    assert not seen                              # the VAD hears nothing while Nova speaks


def test_fallback_can_be_disabled(monkeypatch):
    monkeypatch.setattr("cabin.frontend.aec.hardware_aec_present", lambda cfg: False)
    n = 14 * SR
    ref, other = speechlike(n, 17), speechlike(n, 18)
    tap = audio.ReferenceTap()
    fe = _frontend({"AEC": "on", "AEC_FALLBACK": "none"}, tap, [])
    _drive(fe, tap, other, ref)
    assert fe.mode == "full_duplex"


def test_frontend_calibrates_long_delay(monkeypatch):
    monkeypatch.setattr("cabin.frontend.aec.hardware_aec_present", lambda cfg: False)
    n = 8 * SR
    ref = speechlike(n, 19)
    echo = echo_of(ref, 400, -10)               # beyond the 256 ms filter tail
    noise = (np.random.default_rng(20).standard_normal(n) * 1e-4).astype(np.float32)
    tap = audio.ReferenceTap()
    fe = _frontend({"AEC": "on"}, tap, [])
    _drive(fe, tap, echo + noise, ref)
    assert abs(fe.stats["aec_delay_ms"] - 400) < 150
    assert fe.stats["erle_db"] > 15.0


def test_stage_classes_exposed():
    assert AecStage and ResidualGateStage


# ── playback reference ──────────────────────────────────────────────────────────────

def test_reference_tap_is_continuous_across_blocks():
    tap = audio.ReferenceTap()
    sr_in = 44100
    t = np.arange(sr_in) / sr_in
    tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    rng = np.random.default_rng(21)
    pos, start = 0, 5000.0
    while pos < len(tone):
        k = int(rng.integers(300, 1500))
        blk = tone[pos:pos + k]
        tap.write(start + pos / sr_in, blk, sr_in)
        pos += len(blk)
    end = round((start + 1.0) * SR)
    got = tap.read_at(end, SR - 400)
    ideal = 0.5 * np.sin(2 * np.pi * 440 * (np.arange(end - len(got), end) / SR - start))
    assert np.max(np.abs(got[40:-40] - ideal[40:-40])) < 0.03       # no 1-sample holes or overlaps


def test_reference_tap_gap_and_wraparound_without_big_allocations(monkeypatch):
    tap = audio.ReferenceTap()
    calls = []
    real = np.arange
    monkeypatch.setattr(np, "arange", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    x = np.ones(1600, np.float32)
    tap.write(100.0, x, SR)
    tap.write(100.0 + 1.5, x, SR)                  # 1.4 s of silence in between
    tap.write(100.0 + 3.9, x, SR)                  # gap larger than the ring
    tap.write(100.0 + 4.0, 2 * x, SR)              # wraps
    big = [a for a in calls if a and isinstance(a[-1], (int, np.integer)) and a[-1] > 5000]
    assert not big
    end = round((100.0 + 4.1) * SR)
    out = tap.read_at(end, 3200)
    assert np.allclose(out[:1600], 1.0) and np.allclose(out[1600:], 2.0)
    mid = tap.read_at(round((100.0 + 3.8) * SR), 1600)
    assert not mid.any()


# ── barge-in end to end ─────────────────────────────────────────────────────────────

def _scene(seconds, seed, noise_rms, path_ms=80, gain_db=-10):
    n = int(seconds * SR)
    ref = speechlike(n, seed)
    echo = echo_of(ref, path_ms, gain_db)
    noise = (np.random.default_rng(seed + 100).standard_normal(n) * noise_rms).astype(np.float32)
    return ref, echo, noise


def _vad_frontend(settings, tap, events):
    from cabin.audio import EnergyVAD, VADConfig
    vad = EnergyVAD(VADConfig(sensitivity=1.0))
    fe = FrontEnd(Settings(settings), events.append, reference=tap, stages=[VadStage(vad.update, vad.reset)])
    return fe, vad


def test_driver_over_nova_starts_a_turn_and_echo_alone_does_not(monkeypatch):
    monkeypatch.setattr("cabin.frontend.aec.hardware_aec_present", lambda cfg: False)
    noise_rms = 1e-3
    idle = np.zeros(SR, np.float32)
    ref, echo, noise = _scene(10, 40, noise_rms)
    ref = np.concatenate([idle, ref])
    echo = np.concatenate([idle, echo])
    noise = np.concatenate([(np.random.default_rng(41).standard_normal(SR) * noise_rms).astype(np.float32), noise])

    events: list = []
    tap = audio.ReferenceTap()
    fe, _ = _vad_frontend({"AEC": "on"}, tap, events)
    _drive(fe, tap, echo + noise, ref)
    assert not [e for e in events if e.kind == "start"]          # echo alone: no turn in 10 s of playback

    events.clear()
    tap = audio.ReferenceTap()
    fe, vad = _vad_frontend({"AEC": "on"}, tap, events)
    drv = np.zeros(len(ref), np.float32)
    a = 6 * SR
    drv[a:a + int(0.9 * SR)] = voiced(int(0.9 * SR), 1.2 * noise_rms * 10 ** (12 / 20))
    _drive(fe, tap, echo + noise + drv, ref)
    starts = [e for e in events if e.kind == "start"]
    assert starts and starts[0].t >= T0 + 6.0                     # the driver's voice, not the echo
    assert vad.echo_gain_db == -20.0                              # nothing learned from the gated driver


def test_gate_stage_passes_frames_without_playback_level():
    from cabin.frontend.worker import Context
    gate = ResidualGate()
    stage = ResidualGateStage(gate)
    ctx = Context(playback_db=-20.0, t=0.0, stats={})
    assert stage.process(np.ones(B, np.float32) * 1e-3, ctx) is None     # closed: warm-up
    for _ in range(40):
        stage.process(np.ones(B, np.float32) * 1e-3, ctx)
    loud = np.ones(B, np.float32) * 0.2
    out = None
    for _ in range(20):
        out = stage.process(loud, ctx)
    assert out is not None and ctx.playback_db is None


# ── capture timestamps ──────────────────────────────────────────────────────────────

class _TI:
    def __init__(self, adc, cur):
        self.inputBufferAdcTime, self.currentTime = adc, cur


def _mic(monkeypatch, sr=48000):
    monkeypatch.setattr(audio, "sd", None)
    got = []
    m = audio.Microphone(got.append)
    m.sr = sr
    return m, got


def test_mic_frames_are_stamped_from_the_device_clock(monkeypatch):
    m, got = _mic(monkeypatch)
    rng = np.random.default_rng(1)
    adc = 500.0
    for _ in range(40):
        n = 1000
        m._cb(np.zeros((n, 1), np.float32), n, _TI(adc, adc + rng.uniform(0.0, 0.004)), None)
        adc += n / 48000
    assert len(got) >= 30 and all(isinstance(f, audio.Frame) for f in got)
    d = np.diff([f.t for f in got])
    assert np.allclose(d, 0.02, atol=1e-6)                       # no callback jitter in the stamps
    off = m._clock_off
    # the last sample of frame k is at stream time (k+1)*20 ms after the first sample
    assert abs(got[0].t - (500.0 + off + 0.02)) < 1e-3


def test_mic_frames_without_device_clock_use_a_sample_counter(monkeypatch):
    m, got = _mic(monkeypatch, sr=16000)
    for i in range(30):
        if i % 7 == 3:
            time.sleep(0.01)                                      # a late callback must not move the stamps
        m._cb(np.zeros((320, 1), np.float32), 320, _TI(0.0, 0.0), None)
    assert np.allclose(np.diff([f.t for f in got]), 0.02, atol=1e-9)


def test_mic_clock_resyncs_after_a_gap(monkeypatch):
    m, got = _mic(monkeypatch, sr=16000)
    adc = 100.0
    for i in range(10):
        m._cb(np.zeros((320, 1), np.float32), 320, _TI(adc, adc + 0.001), None)
        adc += 0.02
    adc += 0.5                                                    # xrun
    for i in range(5):
        m._cb(np.zeros((320, 1), np.float32), 320, _TI(adc, adc + 0.001), None)
        adc += 0.02
    assert got[-1].t - got[9].t > 0.5


def test_front_end_uses_the_frame_stamp():
    fe = FrontEnd(Settings({"AEC": "off"}), lambda e: None, stages=[])
    f = np.zeros(B, np.float32).view(audio.Frame)
    f.t = 1234.5
    fe.push(f, None)
    frame, _pb, t, _seq = fe._q.get_nowait()
    assert t == 1234.5 and type(frame) is np.ndarray


# ── robustness ──────────────────────────────────────────────────────────────────────

def test_aec_recovers_after_echo_path_change():
    n = 14 * SR
    ref = speechlike(n, 50)
    noise = (np.random.default_rng(51).standard_normal(n) * 1e-4).astype(np.float32)
    echo = np.concatenate([echo_of(ref, 80, -10)[:6 * SR], echo_of(ref, 140, -6)[6 * SR:]])
    out = run_aec(Aec(), echo + noise, ref)
    assert erle_db(echo, out, noise, 5 * SR, 6 * SR) >= 18.0
    assert erle_db(echo, out, noise, 13 * SR, 14 * SR) >= 15.0


def test_aec_resets_after_non_finite_state():
    aec = Aec()
    ref = speechlike(B, 52)
    out = aec.process(np.full(B, np.nan, np.float32), ref)
    assert np.isfinite(out).all()
    aec.W[:] = np.nan
    out = aec.process(speechlike(B, 53), ref)
    assert np.isfinite(out).all() and np.isfinite(aec.W).all()
    assert np.isfinite(aec.process(speechlike(B, 54), ref)).all()


def test_aec_weights_stay_bounded_on_long_run():
    n = 20 * SR
    ref = speechlike(n, 55)
    aec = Aec()
    run_aec(aec, echo_of(ref, 80, -10), ref)
    assert float(np.abs(aec.W).max()) < 50.0


def test_road_noise_does_not_trigger_half_duplex(monkeypatch):
    monkeypatch.setattr("cabin.frontend.aec.hardware_aec_present", lambda cfg: False)
    idle = SR + SR // 2
    ref, echo, noise = _scene(10, 60, 1e-2, path_ms=80, gain_db=-20)        # -40 dBFS road noise, -20 dBFS Nova
    zeros = np.zeros(idle, np.float32)
    pre = (np.random.default_rng(61).standard_normal(idle) * 1e-2).astype(np.float32)
    tap = audio.ReferenceTap()
    fe = _frontend({"AEC": "on"}, tap, [])
    _drive(fe, tap, np.concatenate([pre, echo + noise]), np.concatenate([zeros, ref]))
    assert fe.mode == "full_duplex"
    assert fe.stats["noise_floor_db"] < -35.0


def test_half_duplex_clears_when_the_echo_is_cancelled_again(monkeypatch):
    monkeypatch.setattr("cabin.frontend.aec.hardware_aec_present", lambda cfg: False)
    ref = speechlike(34 * SR, 62)
    other = speechlike(14 * SR, 63)
    echo = echo_of(ref[14 * SR:], 80, -10)
    noise = (np.random.default_rng(64).standard_normal(34 * SR) * 1e-4).astype(np.float32)
    mic = np.concatenate([other, echo]) + noise
    tap = audio.ReferenceTap()
    fe = _frontend({"AEC": "on"}, tap, [])
    _drive(fe, tap, mic, ref, 0, 14 * SR // B)
    assert fe.mode == "half_duplex"
    _drive(fe, tap, mic, ref, 14 * SR // B, 34 * SR // B)
    assert fe.mode == "full_duplex"


# ── gate model ──────────────────────────────────────────────────────────────────────

def test_gate_follows_a_syllabic_reference_without_opening():
    rng = np.random.default_rng(70)
    gate = ResidualGate()
    t = np.arange(500) * 0.02
    play_db = -40.0 + 15.0 * (1 + np.sin(2 * np.pi * 4.0 * t))              # 4 Hz, -40 .. -10 dBFS
    noise_p = 10 ** (-75 / 10)
    opened = []
    for pdb in play_db:
        p = noise_p + 10 ** (-35 / 10) * 10 ** (pdb / 10)                    # leftover echo 35 dB down
        opened.append(gate.allow(10 * np.log10(p) + rng.normal(0, 0.3), float(pdb), True))
    assert not any(opened)
    # somebody talks over Nova: +13 dB over what the echo model expects
    got = None
    for k in range(25):
        pdb = float(play_db[k])
        exp = gate.expected_db(pdb)
        p = 10 ** (exp / 10) * (1 + 10 ** 1.3)
        if gate.allow(10 * np.log10(p), pdb, True) and got is None:
            got = (k + 1) * 20
    assert got is not None and got <= 320


def test_gate_keeps_its_model_between_sentences():
    gate = ResidualGate()
    for _ in range(60):
        gate.allow(-60.0, -20.0, True)
    for _ in range(30):                                                      # 0.6 s between sentences
        assert gate.allow(-60.0, -90.0, False)
    assert gate.noise_p is not None
    got = None
    for k in range(25):                                                      # no warm-up again
        if gate.allow(-40.0, -20.0, True) and got is None:
            got = (k + 1) * 20
    assert got is not None and got <= 320
    for _ in range(80):                                                      # a real pause: forget
        gate.allow(-60.0, -90.0, False)
    assert gate.noise_p is None


def test_gate_close_does_not_reset_a_running_turn():
    from cabin.frontend.worker import Context
    for busy, expected in ((True, 0), (False, 1)):
        closed: list = []
        stage = ResidualGateStage(ResidualGate(), None, lambda c=closed: c.append(1), lambda busy=busy: busy)
        ctx = Context(playback_db=-20.0, t=0.0, stats={})
        stage.process(np.zeros(B, np.float32), ctx)
        assert len(closed) == expected


def test_speaking_follows_the_reference_not_the_player(monkeypatch):
    monkeypatch.setattr("cabin.frontend.aec.hardware_aec_present", lambda cfg: False)
    n = 4 * SR
    ref = np.concatenate([speechlike(2 * SR, 71), np.zeros(2 * SR, np.float32)])
    echo = echo_of(ref, 80, -10)
    tap = audio.ReferenceTap()
    fe = _frontend({"AEC": "on"}, tap, [])
    aec, gate_open = fe.stages[0], []
    speaking = []
    for i in range(n // B):
        s = slice(i * B, (i + 1) * B)
        for j in range(i if i == 0 else i + 2, min(i + 3, n // B)):
            tap.write(T0 + j * 0.02, ref[j * B:(j + 1) * B], SR)
        fe.process(echo[s], None, T0 + (i + 1) * 0.02)
        speaking.append(aec.speaking)
        gate_open.append(fe.stats.get("gate_open"))
    assert all(speaking[10:95])                                              # while the reference plays
    last_ref = 2 * SR // B
    assert all(speaking[last_ref:last_ref + aec.aec.P])                      # hangover of the tail length
    assert not speaking[-1]
    assert gate_open[60] is False and gate_open[-1] is True


# ── timing window and early echo ────────────────────────────────────────────────────

def test_delay_search_window():
    ref = speechlike(2 * SR, 72)
    mic = echo_of(ref, 300, -8)
    full = estimate_delay_ex_(mic, ref)
    assert abs(full - int(0.300 * SR)) <= 160
    narrow = estimate_delay_ex_(mic, ref, lo=100, hi=200)
    assert int(0.100 * SR) <= narrow <= int(0.200 * SR)


def estimate_delay_ex_(mic, ref, lo=0.0, hi=1000.0):
    from cabin.frontend.aec import estimate_delay_ex
    return estimate_delay_ex(mic, ref, SR, hi, lo)[0]


def test_calibration_looks_around_the_player_latency_first(monkeypatch):
    monkeypatch.setattr("cabin.frontend.aec.hardware_aec_present", lambda cfg: False)
    import cabin.frontend.aec as mod
    calls = []
    real = mod.estimate_delay_ex
    monkeypatch.setattr(mod, "estimate_delay_ex", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    ref, echo, noise = _scene(4, 73, 1e-4, path_ms=230, gain_db=-10)
    tap = audio.ReferenceTap()
    tap.delay_fn = lambda: 0.1
    fe = _frontend({"AEC": "on"}, tap, [])
    _drive(fe, tap, echo + noise, ref)
    assert calls and calls[0][3] <= 400 and calls[0][4] >= 0.0 and calls[0][4] < calls[0][3]
    assert len(calls) == 1                                                   # found inside the window
    assert abs(fe.stats["aec_delay_ms"] - (230 + 20 - 80)) < 40


def test_early_echo_is_modelled(monkeypatch):
    monkeypatch.setattr("cabin.frontend.aec.hardware_aec_present", lambda cfg: False)
    ref, echo, noise = _scene(5, 74, 1e-4, path_ms=0, gain_db=-8)
    tap = audio.ReferenceTap()
    fe = _frontend({"AEC": "on"}, tap, [])
    _drive(fe, tap, echo + noise, ref, stamp_shift=-0.010)                   # the stamp runs 10 ms behind
    assert fe.stats["erle_db"] > 18.0


# ── fix round 2 ─────────────────────────────────────────────────────────────────────

def pausey(n: int, seed: int) -> np.ndarray:
    """Speech-like reference with 40 dB pauses between 250-500 ms phrases."""
    rng = np.random.default_rng(seed)
    x = speechlike(n, seed)
    g = np.ones(n, np.float32)
    i, talk = 0, True
    while i < n:
        k = int(SR * (rng.uniform(0.25, 0.5) if talk else rng.uniform(0.15, 0.4)))
        if not talk:
            g[i:i + k] = 0.01
        i, talk = i + k, not talk
    return (x * g).astype(np.float32)


def _step(fe, tap, mic, ref, i, ahead=2):
    total = len(mic) // B
    for j in range(i if i == 0 else i + ahead, min(i + ahead + 1, total)):
        tap.write(T0 + j * 0.02, ref[j * B:(j + 1) * B], SR)
    fe.process(mic[i * B:(i + 1) * B], None, T0 + (i + 1) * 0.02)


@pytest.mark.parametrize("seed", [3, 4])
def test_gate_uses_delay_aligned_playback_power(monkeypatch, seed):
    monkeypatch.setattr("cabin.frontend.aec.hardware_aec_present", lambda cfg: False)
    n = 14 * SR
    ref = pausey(n, seed)
    echo = echo_of(ref, 150, -10)
    noise = (np.random.default_rng(seed + 9).standard_normal(n) * 1e-3).astype(np.float32)
    a = 11 * SR // B
    peak = None
    for driver in (False, True):
        tap = audio.ReferenceTap()
        fe = _frontend({"AEC": "on"}, tap, [])
        aec, gate = fe.stages[0], fe.stages[1].gate
        mic = echo + noise
        if driver:
            mic = mic.copy()
            mic[11 * SR:11 * SR + int(0.9 * SR)] += voiced(int(0.9 * SR), 10 ** ((peak + 12) / 20))
        opened_early, opened_at, exp = 0, None, []
        for i in range(n // B):
            _step(fe, tap, mic, ref, i)
            if i < a and fe.stats.get("gate_open") and aec.speaking:
                opened_early += 1
            if i < a and i > a - 150 and aec.speaking:
                exp.append(gate.expected_db(aec.ref_db))
            if driver and i >= a and opened_at is None and fe.stats.get("gate_open") and aec.speaking:
                opened_at = (i - a + 1) * 20
        assert opened_early == 0                       # no false opens, not even while the filter converges
        assert gate.k < 0.05                           # the model did not learn pauses as echo
        if not driver:
            peak = max(exp)
    assert opened_at is not None and opened_at <= 500


def _gate_with_model():
    g = ResidualGate()
    for _ in range(60):
        g.allow(-58.0, -20.0, True)
    return g


def test_gate_does_not_learn_while_an_onset_is_pending():
    g = _gate_with_model()
    k0, n0 = g.k, g.noise_p
    exp = g.expected_db(-20.0)
    opened = None
    seq = [exp + 13] * 8 + [exp + 5] * 3 + [exp + 13] * 10                # the level dips mid-word
    for i, r in enumerate(seq):
        if g.allow(r, -20.0, True) and opened is None:
            opened = i + 1
    assert g.k == k0 and g.noise_p == n0
    assert opened is not None and opened <= 21                           # 8 up, 3 down (-1 each), 10 up = 15


def test_gate_gives_a_short_strong_onset_a_short_hold():
    g = _gate_with_model()
    exp = g.expected_db(-20.0)
    got = None
    for i in range(20):
        if g.allow(exp + 22, -20.0, True) and got is None:
            got = (i + 1) * 20
    assert got is not None and got <= 140


def test_stop_burst_starts_a_turn_at_gate_open(monkeypatch):
    monkeypatch.setattr("cabin.frontend.aec.hardware_aec_present", lambda cfg: False)
    noise_rms = 1e-3
    ref, echo, noise = _scene(10, 80, noise_rms)
    rng = np.random.default_rng(81)
    burst = np.concatenate([
        (rng.standard_normal(int(0.06 * SR)) * 1.5e-2).astype(np.float32),                  # plosive
        voiced(int(0.2 * SR), 1.2 * noise_rms * 10 ** (12 / 20)),                           # vowel
        (rng.standard_normal(int(0.12 * SR)) * 2.4e-3).astype(np.float32)])                 # weak sibilant
    assert abs(len(burst) / SR - 0.38) < 0.001
    drv = np.zeros(len(ref), np.float32)
    drv[6 * SR:6 * SR + len(burst)] = burst
    events: list = []
    tap = audio.ReferenceTap()
    fe, _ = _vad_frontend({"AEC": "on"}, tap, events)
    _drive(fe, tap, echo + noise + drv, ref)
    starts = [e for e in events if e.kind == "start"]
    assert starts and 6.0 <= starts[0].t - T0 <= 6.0 + 0.38 + 0.02      # fires inside the burst, at gate open


def test_running_turn_sees_its_end_when_the_gate_closes():
    from cabin.frontend.worker import Context
    stage = ResidualGateStage(ResidualGate(), None, None, lambda: True)
    ctx = Context(playback_db=-20.0, t=0.0, stats={})
    f = np.zeros(B, np.float32)
    assert stage.process(f, ctx) is f
    quiet = ResidualGateStage(ResidualGate(), None, None, lambda: False)
    assert quiet.process(f, Context(playback_db=-20.0, t=0.0, stats={})) is None


def test_baseline_is_not_reseeded_while_the_driver_talks():
    n = 12 * SR
    ref = speechlike(n, 90)
    echo = echo_of(ref, 80, -10)
    drv = np.zeros(n, np.float32)
    drv[5 * SR:11 * SR] = voiced(6 * SR, 0.1)
    base = {}
    for hold in (True, False):
        aec = Aec()
        for i in range(n // B):
            s = slice(i * B, (i + 1) * B)
            aec.hold_baseline = hold and 5 * SR <= i * B < 11 * SR
            aec.process((echo + drv)[s], ref[s])
            if i * B == 9 * SR - (9 * SR % B):
                base[hold] = aec._r_base
    assert base[True] < 0.1 < base[False]


def test_mic_counter_is_reanchored_against_the_clock(monkeypatch):
    m, got = _mic(monkeypatch, sr=16000)
    clock, jitter = [5000.0], [0.0]
    monkeypatch.setattr(audio.time, "monotonic", lambda: clock[0] + jitter[0])
    rng = np.random.default_rng(5)
    for i in range(3300):                                        # 66 s; the device runs 0.1 % slow
        clock[0] += 0.02 * 1.001
        jitter[0] = float(rng.uniform(0.0, 0.03))                # callbacks arrive up to 30 ms late
        m._cb(np.zeros((320, 1), np.float32), 320, _TI(0.0, 0.0), None)
    assert abs(got[-1].t - clock[0]) < 0.03
    # a single late reading must not move the anchor: no drift, jittery callbacks
    m2, got2 = _mic(monkeypatch, sr=16000)
    clock[0] = 9000.0
    for i in range(3200):
        clock[0] += 0.02
        jitter[0] = 0.0 if i % 40 else 0.05                      # one very late callback every 40
        m2._cb(np.zeros((320, 1), np.float32), 320, _TI(0.0, 0.0), None)
    assert abs(got2[-1].t - clock[0]) < 0.01


# ── fix round 3 ─────────────────────────────────────────────────────────────────────

def test_aec_converges_with_strong_coupling():
    n = 4 * SR
    ref = speechlike(n, 95, rms=0.05)
    echo = echo_of(ref, 80, 6.0)                                  # the mic hears the speaker 6 dB hotter
    noise = (np.random.default_rng(96).standard_normal(n) * 1e-4).astype(np.float32)
    out = run_aec(Aec(), echo + noise, ref)
    assert erle_db(echo, out, noise, 2 * SR, 4 * SR) >= 18.0


def test_vad_receives_each_frame_once_across_a_mid_turn_close():
    from cabin.frontend.worker import Context

    class Vad:
        def __init__(self):
            self.seen = []

        def process(self, frame, ctx):
            self.seen.append(id(frame))
            return frame

    vad, state, frames = Vad(), {"busy": False}, []
    gate = ResidualGate()
    stage = ResidualGateStage(gate, None, None, lambda: state["busy"], vad)

    def feed(level):
        frames.append(np.full(B, level, np.float32))
        return frames[-1], stage.process(frames[-1], Context(playback_db=-20.0, t=0.0, stats={}))

    for _ in range(60):                         # Nova speaking, the gate learns and holds the echo back
        feed(1e-3)
    state["busy"] = True                        # a turn is running: closed-gate frames go straight through
    passed = []
    for _ in range(10):
        f, out = feed(1e-3)
        assert out is f
        passed.append(id(f))
    state["busy"] = False
    held = []
    out = live = None
    for _ in range(25):                         # the driver talks over Nova: the gate opens and replays
        live, out = feed(0.2)
        if out is not None:
            break
        held.append(id(live))
    assert gate.is_open and out is live
    assert not set(passed) & set(vad.seen)      # frames the VAD already got are not replayed
    assert vad.seen == held                     # the held-back frames, once each, in order
