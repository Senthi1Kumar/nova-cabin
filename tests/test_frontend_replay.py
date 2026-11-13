"""Replay of recorded hall audio through the front end: turn starts and transcription accuracy.

Fixtures live in ``tests/fixtures/audio/<DEVICE>/`` (see the README there) and are written by
``tools/record_fixtures.py``. Without them the replay tests skip; the harness itself is checked
on synthetic clips so it cannot rot unnoticed.
"""
from __future__ import annotations

import functools
import importlib.util
import json
import re
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from cabin.audio import FRAME, MIC_SR, ReferenceTap, VADConfig, VadStage, rms_db
from cabin.config import Settings
from cabin.frontend import FrontEnd

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "audio"
T0 = 1000.0
AHEAD = 2                         # frames the reference reaches the tap before the mic frame, as the player does
START_TOLERANCE = 1
MAX_WER = 0.25

# Echo handling per capture device: the XVF3800 cancels in hardware, the WM8960 path uses the software canceller.
PROFILES = {"XVF3800": {"AEC": "off"}, "WM8960": {"AEC": "on"}}


def _recorder():
    spec = importlib.util.spec_from_file_location("record_fixtures", ROOT / "tools" / "record_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── helpers ─────────────────────────────────────────────────────────────────────────

def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == MIC_SR and w.getnchannels() == 1 and w.getsampwidth() == 2
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0


def write_wav(path: Path, x: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(MIC_SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["_", "_", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _number_words(m: re.Match) -> str:
    n = int(m.group(0))
    if n < 20:
        return _ONES[n]
    return _TENS[n // 10] + (f" {_ONES[n % 10]}" if n % 10 else "")


def normalise(text: str) -> list[str]:
    text = re.sub(r"\b\d{1,2}\b", _number_words, text.lower().replace("centre", "center"))
    return re.sub(r"[^a-z' ]+", " ", text).split()


def wer(ref: str, hyp: str) -> float:
    r, h = normalise(ref), normalise(hyp)
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
    return d[len(h)] / max(1, len(r))


def replay(mic: np.ndarray, ref: np.ndarray | None, aec: str, vad_engine: str = "auto", *,
           wake_with=None) -> list:
    """Push ``mic`` through a front end shaped like the live one; returns the events it produced.

    ``wake_with`` is a Parakeet engine: when given, the wake phrase stage is on and checks the
    start of each utterance with it, as the app does.
    """
    from cabin import frontend
    from cabin.audio import VAD
    vad = VAD(VADConfig(), engine=vad_engine, threaded=False)
    events: list = []
    settings = {"AEC": aec}
    if wake_with is not None:
        settings.update(WAKE_WORD="hey nova", DRIVER_GATE="off")
    saved = frontend.wake_required, frontend.wake_transcriber
    if wake_with is not None:
        frontend.wake_required = lambda: True
        frontend.wake_transcriber = lambda: wake_with.transcribe_sync
    try:
        fe = FrontEnd(Settings(settings), events.append, reference=ReferenceTap(),
                      stages=[VadStage(vad._update_sync, vad._reset_state)])
    finally:
        frontend.wake_required, frontend.wake_transcriber = saved
    tap = fe.reference
    wake = [st for st in fe.stages if hasattr(st, "_job")]
    n = len(mic) // FRAME
    env = -90.0
    for i in range(n):
        if ref is not None:
            for j in range(i if i == 0 else i + AHEAD, min(i + AHEAD + 1, n)):
                tap.write(T0 + j * 0.02, ref[j * FRAME:(j + 1) * FRAME], MIC_SR)
            db = rms_db(ref[i * FRAME:(i + 1) * FRAME])
            env = db if db > env else env + (db - env) * 0.3
        fe.process(mic[i * FRAME:(i + 1) * FRAME], env if ref is not None else None, T0 + (i + 1) * 0.02, i + 1)
        for st in wake:                                  # the recogniser runs in real time live; here, wait for it
            if st._job is not None:
                st._job[0].result()
    return events


def starts(events) -> int:
    return sum(e.kind == "start" for e in events)


def _burst(seconds: float, rms: float = 0.1, f0: float = 140.0) -> np.ndarray:
    t = np.arange(int(seconds * MIC_SR)) / MIC_SR
    x = sum(np.sin(2 * np.pi * f0 * k * t + k) / k for k in range(1, 12))
    return (x * rms / np.sqrt(np.mean(x ** 2))).astype(np.float32)


def _quiet(seconds: float, rms: float = 1e-3, seed: int = 0) -> np.ndarray:
    return (np.random.default_rng(seed).standard_normal(int(seconds * MIC_SR)) * rms).astype(np.float32)


# ── harness checks on synthetic clips ───────────────────────────────────────────────

def test_wer_and_normalisation():
    assert wer("yes stop cancel two approve", "Yes. Stop. Cancel. 2. Approve.") == 0.0
    assert wer("find a shopping centre", "find a shopping center") == 0.0
    assert wer("a b c d", "a x c") == pytest.approx(0.5)
    assert normalise("twenty degrees") == normalise("20 degrees")


def test_replay_counts_turn_starts_on_a_synthetic_clip():
    mic = np.concatenate([_quiet(1.5, seed=1), _burst(1.2), _quiet(1.5, seed=2), _burst(1.2), _quiet(1.5, seed=3)])
    assert starts(replay(mic, None, "off", "energy")) == 2
    assert starts(replay(_quiet(8, seed=4), None, "off", "energy")) == 0


def test_replay_with_reference_runs_the_software_canceller():
    n = 6 * MIC_SR
    ref = np.concatenate([np.zeros(MIC_SR, np.float32), _burst(5.0, 0.1)])[:n]
    mic = np.concatenate([np.zeros(80 * MIC_SR // 1000, np.float32), ref * 0.3])[:n] + _quiet(6, seed=5)
    assert starts(replay(mic, ref, "on", "energy")) == 0


def test_recorder_manifest_covers_every_clip_and_names_expected_decisions():
    rec = _recorder()
    names = [c.name for c in rec.CLIPS]
    assert names == ["clean_speech", "speech_over_nova", "tv_speech", "music", "fan", "two_people", "short_words"]
    by = {c.name: c for c in rec.CLIPS}
    assert by["speech_over_nova"].plays_reference
    assert by["clean_speech"].transcript and by["short_words"].transcript
    assert all(by[n].expected_starts == 0 for n in ("fan",))
    m = rec.build_manifest("WM8960", {"fan": 18.0})
    assert m["device"] == "WM8960" and m["sample_rate"] == MIC_SR
    assert m["clips"]["fan"]["file"] == "fan.wav" and m["clips"]["fan"]["expected_starts"] == 0


# ── recorded fixtures ───────────────────────────────────────────────────────────────

def _cases(root: Path = FIXTURES, *, skip_if_empty: bool = True) -> list:
    out = []
    for manifest in sorted(root.glob("*/manifest.json")):
        data = json.loads(manifest.read_text())
        for name, clip in data["clips"].items():
            if (manifest.parent / clip["file"]).is_file():
                out.append(pytest.param(manifest.parent, data["device"], name, clip, id=f"{data['device']}-{name}"))
    if out or not skip_if_empty:
        return out
    return [pytest.param(None, None, None, None, id="no-fixtures",
                         marks=pytest.mark.skip(reason="no recorded fixtures; run tools/record_fixtures.py"))]


def check_starts(folder: Path, device: str, name: str, clip: dict, vad_engine: str = "auto", stt=None) -> None:
    if clip.get("needs_wake") and stt is None:
        pytest.skip("speech that is not addressed to Nova is only rejected by the wake stage (needs Parakeet)")
    mic = read_wav(folder / clip["file"])
    ref = read_wav(folder / clip["ref"]) if clip.get("ref") and (folder / clip["ref"]).is_file() else None
    if ref is not None:
        ref = np.pad(ref, (0, max(0, len(mic) - len(ref))))[:len(mic)]
    wake = stt if name != "short_words" else None
    got = starts(replay(mic, ref, PROFILES.get(device, {"AEC": "auto"})["AEC"], vad_engine, wake_with=wake))
    expected = clip["expected_starts"]
    assert abs(got - expected) <= START_TOLERANCE, f"{device}/{name}: {got} turn starts, expected {expected}"


@pytest.mark.parametrize("folder, device, name, clip", _cases())
def test_recorded_clip_turn_starts(folder, device, name, clip):
    check_starts(folder, device, name, clip, stt=_parakeet_or_none())


def test_recorded_layout_is_replayed_end_to_end(tmp_path):
    rec = _recorder()
    folder = tmp_path / "WM8960"
    folder.mkdir()
    speech = np.concatenate([_quiet(1.5, seed=6), _burst(1.2), _quiet(2.0, seed=7)])
    ref = np.concatenate([np.zeros(MIC_SR, np.float32), _burst(2.0, 0.1, f0=200.0), np.zeros(MIC_SR, np.float32)])
    write_wav(folder / "clean_speech.wav", speech)
    write_wav(folder / "fan.wav", _quiet(5.0, 2e-3, seed=8))
    write_wav(folder / "speech_over_nova.wav", np.concatenate([ref[:MIC_SR], ref[MIC_SR:] * 0.2, _quiet(1.0, seed=9)])[:len(ref)])
    write_wav(folder / "speech_over_nova_ref.wav", ref)
    manifest = rec.build_manifest("WM8960", {"clean_speech": 4.7, "fan": 5.0, "speech_over_nova": 4.0})
    (folder / "manifest.json").write_text(json.dumps(manifest))
    cases = _cases(tmp_path, skip_if_empty=False)
    assert len(cases) == 3
    for c in cases:
        _folder, device, name, clip = c.values
        if name == "speech_over_nova":
            clip = {**clip, "expected_starts": 0}          # echo only: the canceller must keep it from starting a turn
        check_starts(_folder, device, name, clip, "energy")


@functools.lru_cache(maxsize=1)
def _parakeet_or_none():
    from cabin.providers.stt_parakeet import load
    return load("", "", False, True)


@pytest.fixture
def parakeet():
    stt = _parakeet_or_none()
    if stt is None:
        pytest.skip("Parakeet model not present")
    return stt


COMMAND_WORDS = ["yes", "stop", "cancel", "two", "approve"]
PREROLL_FRAMES = 23


def segments(mic: np.ndarray, events) -> list[np.ndarray]:
    """The audio of each utterance the front end's VAD found, with the engine's pre-roll."""
    st = [e for e in events if e.kind == "start"]
    en = [e for e in events if e.kind == "end"]
    out = []
    for s_ev, e_ev in zip(st, en, strict=False):
        a = max(0, round((s_ev.t - T0) / 0.02) - PREROLL_FRAMES)
        out.append(mic[a * FRAME:round((e_ev.t - T0) / 0.02) * FRAME])
    return out


def hear_each(stt, parts: list[np.ndarray], vocabulary: list[str]) -> list[str]:
    """Each segment through a Parakeet session with the vocabulary hook and transcript gate, as the engine does."""
    import asyncio

    from cabin import frontend
    from cabin.audio import to_pcm16

    async def one(x):
        session = stt.session()
        await session.feed(to_pcm16(x))
        return await session.finish()

    saved = frontend.vocabulary_provider
    frontend.vocabulary_provider = lambda: vocabulary
    try:
        return [asyncio.run(one(x)) for x in parts]
    finally:
        frontend.vocabulary_provider = saved


def _wer_cases():
    return [c for c in _cases() if c.values[3] is None or (c.values[3].get("transcript") and c.values[2] != "short_words")]


@pytest.mark.parametrize("folder, device, name, clip", _wer_cases())
def test_recorded_clip_wer(parakeet, folder, device, name, clip):
    from cabin.audio import to_pcm16
    text = parakeet.transcribe_sync(to_pcm16(read_wav(folder / clip["file"])))
    score = wer(clip["transcript"], text)
    assert score <= MAX_WER, f"{device}/{name}: WER {score:.2f} for {text!r}"


@pytest.mark.xfail(reason="Parakeet mishears isolated 'stop' and 'cancel'; see the A6 report for the proposed fix", strict=False)
@pytest.mark.parametrize("folder, device, name, clip", [c for c in _cases() if c.values[2] in (None, "short_words")])
def test_recorded_short_words_per_segment(parakeet, folder, device, name, clip):
    mic = read_wav(folder / clip["file"])
    parts = segments(mic, replay(mic, None, PROFILES.get(device, {"AEC": "auto"})["AEC"]))
    heard = hear_each(parakeet, parts, COMMAND_WORDS)
    right = sum(i < len(heard) and normalise(heard[i]) == [w] for i, w in enumerate(COMMAND_WORDS))
    assert right >= 4, f"{device}: {right}/5 words right; heard {heard}"
