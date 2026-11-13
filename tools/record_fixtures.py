#!/usr/bin/env python3
"""Record the hall audio fixtures that tests/test_frontend_replay.py replays.

Run it on the Pi with the speakers on: it plays Nova's reference sentence out loud for one clip,
prompts for each take on the terminal, shows a level meter and writes 16 kHz mono WAV files plus a
manifest.json per capture device under ``tests/fixtures/audio/<DEVICE>/``.

    .venv/bin/python tools/record_fixtures.py --list
    .venv/bin/python tools/record_fixtures.py                    # every clip, every device found
    .venv/bin/python tools/record_fixtures.py --only fan --device XVF3800

Both microphones record the same take at once, so each clip is spoken once.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cabin import audio
from cabin.audio import FRAME, MIC_SR, TTS_SR

DEVICES = ("XVF3800", "WM8960")
FIXTURES = ROOT / "tests" / "fixtures" / "audio"
NOVA_SENTENCE_TEXT = ("Sure. I found three coffee shops nearby. The closest is Blue Door Cafe, "
                      "about four minutes away, and I can start navigation whenever you are ready.")
NOVA_DELAY_S = 1.5              # silence before the reference starts, so the take has a clean lead-in
DEFAULT_SECONDS = 18.0


@dataclass(frozen=True)
class Clip:
    name: str
    prompt: str
    expected_starts: int
    transcript: str = ""
    plays_reference: bool = False
    needs_wake: bool = False
    seconds: float = DEFAULT_SECONDS
    note: str = ""


CLIPS: tuple[Clip, ...] = (
    Clip("clean_speech", "Say: 'Hey Nova, set the temperature to twenty degrees and find a shopping centre nearby'",
         1, transcript="Hey Nova, set the temperature to twenty degrees and find a shopping centre nearby",
         note="Speak once, naturally, then stay quiet until the clip ends."),
    Clip("speech_over_nova", "Nova starts talking. Interrupt her by saying: 'Hey Nova, stop'",
         1, plays_reference=True,
         note="Wait for her to be mid-sentence, then cut in."),
    Clip("tv_speech", "Play a YouTube talk on the laptop or phone near the Pi. Do not speak.",
         0, needs_wake=True),
    Clip("music", "Play a song near the Pi. Do not speak.",
         0, needs_wake=True),
    Clip("fan", "Fan on, room silent. Do not speak.", 0),
    Clip("two_people", "Two people chat with each other, not addressing Nova.",
         0, needs_wake=True),
    Clip("short_words", "Say, with a pause of about two seconds between: 'yes' ... 'stop' ... 'cancel' ... 'two' ... 'approve'",
         5, transcript="yes stop cancel two approve", seconds=20.0),
)
BY_NAME = {c.name: c for c in CLIPS}


def build_manifest(device: str, durations: dict[str, float], existing: dict | None = None) -> dict:
    """Manifest for one device; ``existing`` entries for clips not recorded now are kept."""
    clips = dict((existing or {}).get("clips", {}))
    for name, seconds in durations.items():
        c = BY_NAME[name]
        entry: dict = {"file": f"{name}.wav", "seconds": round(seconds, 2),
                       "expected_starts": c.expected_starts, "prompt": c.prompt}
        if c.transcript:
            entry["transcript"] = c.transcript
        if c.plays_reference:
            entry["ref"] = f"{name}_ref.wav"
        if c.needs_wake:
            entry["needs_wake"] = True
        clips[name] = entry
    order = [c.name for c in CLIPS if c.name in clips]
    return {"device": device, "sample_rate": MIC_SR, "clips": {n: clips[n] for n in order}}


def write_wav(path: Path, x: np.ndarray, sr: int = MIC_SR) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(audio.to_pcm16(x))


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0, w.getframerate()


# ── devices ─────────────────────────────────────────────────────────────────────────

def input_device_names() -> list[str]:
    if audio.sd is None:
        return []
    return [str(d["name"]) for d in audio.sd.query_devices() if d.get("max_input_channels", 0) > 0]


def present_devices(requested: str, names: list[str]) -> tuple[list[str], list[str]]:
    """(devices to record, messages). A device counts as present when an input source carries its name."""
    wanted = list(DEVICES) if requested == "all" else [requested]
    found, notes = [], []
    for dev in wanted:
        if any(dev.lower() in n.lower() for n in names):
            found.append(dev)
        else:
            notes.append(f"{dev}: no input device with that name is listed; skipping it")
    return found, notes


# ── reference sentence ──────────────────────────────────────────────────────────────

def nova_sentence_path() -> Path:
    return FIXTURES / "nova_sentence.wav"


def ensure_nova_sentence() -> Path:
    """Nova's reference sentence, synthesised once with Phonon and cached."""
    path = nova_sentence_path()
    if path.is_file():
        return path
    from cabin.providers.tts_phonon import PhononTTS, model_cached
    if not model_cached():
        raise SystemExit(f"{path} is missing and Phonon is not available here to generate it; "
                         "copy the file over or run this once on a machine with Phonon")
    speech = asyncio.run(PhononTTS().synth(NOVA_SENTENCE_TEXT))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TTS_SR)
        w.writeframes(speech.pcm16)
    return path


# ── capture ─────────────────────────────────────────────────────────────────────────

class Channel:
    """One microphone: its frames, and the playback reference sampled on the same frame clock."""

    def __init__(self, device: str, get_reference) -> None:
        self.device = device
        self._get_reference = get_reference
        self.mic: list[np.ndarray] = []
        self.ref: list[np.ndarray] = []
        self.recording = False
        self.db = -90.0
        self.peak = 0.0
        self.index = audio.resolve_device(device)
        self.source = audio.Microphone(self._on_frame, device=self.index)
        self.name = self._resolved_name()

    def _resolved_name(self) -> str:
        try:
            return str(audio.sd.query_devices(self.index, "input")["name"])
        except Exception:                               # noqa: BLE001
            return "?"

    @property
    def available(self) -> bool:
        return self.source.available

    def _on_frame(self, frame: np.ndarray) -> None:
        self.db = audio.rms_db(frame)
        if not self.recording:
            return
        self.peak = max(self.peak, float(np.max(np.abs(frame))))
        self.mic.append(np.array(frame, np.float32))
        tap = self._get_reference()
        t = getattr(frame, "t", None)
        self.ref.append(tap.read_at(round(t * tap.sr), FRAME) if tap is not None and t is not None
                        else np.zeros(FRAME, np.float32))

    def begin(self) -> None:
        self.mic, self.ref, self.peak = [], [], 0.0
        self.recording = True

    def end(self) -> tuple[np.ndarray, np.ndarray]:
        self.recording = False
        return (np.concatenate(self.mic) if self.mic else np.zeros(0, np.float32),
                np.concatenate(self.ref) if self.ref else np.zeros(0, np.float32))

    def close(self) -> None:
        self.source.close()


def meter(db: float, width: int = 24) -> str:
    fill = round(max(0.0, min(1.0, (db + 60.0) / 60.0)) * width)
    return "#" * fill + "." * (width - fill)


def run_take(clip: Clip, channels: list[Channel], player, sentence: np.ndarray | None, seconds: float) -> None:
    for ch in channels:
        ch.begin()
    t_end = time.monotonic() + seconds
    played = False
    t_start = time.monotonic()
    while (now := time.monotonic()) < t_end:
        if clip.plays_reference and not played and now - t_start >= NOVA_DELAY_S:
            player.enqueue(sentence, TTS_SR)
            played = True
        bars = "   ".join(f"{ch.device} [{meter(ch.db)}] {ch.db:6.1f} dB" for ch in channels)
        print(f"\r  REC {t_end - now:4.1f}s   {bars}  ", end="", flush=True)
        time.sleep(0.05)
    print("\r" + " " * 100 + "\r", end="")
    if player is not None:
        player.flush()


def summarise(device: str, mic: np.ndarray, peak: float, clip: Clip) -> None:
    db = audio.rms_db(mic) if len(mic) else -90.0
    warns = []
    if peak >= 0.99:
        warns.append("clipped; lower the speaker or mic gain")
    if clip.name != "fan" and db < -60.0:
        warns.append("very quiet; was anything playing or spoken?")
    print(f"  {device}: {len(mic) / MIC_SR:5.1f} s   rms {db:6.1f} dB   peak {peak:.2f}"
          + ("   WARNING: " + "; ".join(warns) if warns else ""))


def record(args, devices: list[str], clips: list[Clip]) -> int:
    sentence = None
    if any(c.plays_reference for c in clips):
        sentence, sr = read_wav(ensure_nova_sentence())
        assert sr == TTS_SR
    player = audio.Player()
    if not player.available and any(c.plays_reference for c in clips):
        print("No audio output is available, so 'speech_over_nova' cannot be recorded.")
        return 2
    channels = [Channel(d, lambda: player.reference) for d in devices]
    live = [c for c in channels if c.available]
    for c in channels:
        if not c.available:
            print(f"{c.device}: could not open the microphone ({c.source.error}); skipping it")
    if not live:
        print("No microphone could be opened.")
        return 2
    out_root = Path(args.out)
    for c in live:
        shared = c.name.lower() in audio._SHARED_HOSTS
        print(f"{c.device}: capturing from '{c.name}'" + (" (the system default source)" if shared else ""))
    print(f"Recording {len(clips)} clip(s) from {', '.join(c.device for c in live)} into {out_root}/<device>/")
    print("Enter = start the take, s = skip the clip, q = quit.\n")
    manifests = {c.device: json.loads((out_root / c.device / "manifest.json").read_text())
                 if (out_root / c.device / "manifest.json").is_file() else None for c in live}
    try:
        for n, clip in enumerate(clips, 1):
            while True:
                print(f"[{n}/{len(clips)}] {clip.name}\n  {clip.prompt}" + (f"\n  ({clip.note})" if clip.note else ""))
                key = input("  Press Enter to start (s skip, q quit): ").strip().lower()
                if key == "q":
                    return 0
                if key == "s":
                    break
                seconds = args.seconds or clip.seconds
                run_take(clip, live, player, sentence, seconds)
                takes = {ch.device: ch.end() for ch in live}
                for ch in live:
                    summarise(ch.device, takes[ch.device][0], ch.peak, clip)
                if input("  Enter = keep, r = redo: ").strip().lower() == "r":
                    continue
                for ch in live:
                    mic, ref = takes[ch.device]
                    folder = out_root / ch.device
                    write_wav(folder / f"{clip.name}.wav", mic)
                    if clip.plays_reference:
                        write_wav(folder / f"{clip.name}_ref.wav", ref)
                    manifests[ch.device] = build_manifest(ch.device, {clip.name: len(mic) / MIC_SR}, manifests[ch.device])
                    (folder / "manifest.json").write_text(json.dumps(manifests[ch.device], indent=2) + "\n")
                print("  saved\n")
                break
    finally:
        for ch in channels:
            ch.close()
        player.close()
    print("Done. Run: .venv/bin/python -m pytest tests/test_frontend_replay.py -q -p no:cacheprovider")
    return 0


def list_plan(devices: list[str], notes: list[str], clips: list[Clip], out: str) -> None:
    print(f"devices: {', '.join(devices) or 'none found'}")
    for note in notes:
        print(f"  {note}")
    print(f"output: {out}/<device>/   reference: {nova_sentence_path()}"
          f" ({'cached' if nova_sentence_path().is_file() else 'will be generated with Phonon'})")
    for c in clips:
        extra = " [plays Nova]" if c.plays_reference else ""
        extra += f" [transcript: {c.transcript!r}]" if c.transcript else ""
        extra += " [needs wake stage]" if c.needs_wake else ""
        print(f"  {c.name:17s} {c.seconds:4.0f} s  expected starts {c.expected_starts}{extra}\n"
              f"      {c.prompt}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default="all", choices=[*DEVICES, "all"], help="capture device (default: every one found)")
    ap.add_argument("--out", default=str(FIXTURES), help="fixtures root; clips go to <out>/<DEVICE>/")
    ap.add_argument("--only", action="append", default=[], metavar="CLIP", help="record only this clip (repeatable)")
    ap.add_argument("--seconds", type=float, default=0.0, help="clip length override")
    ap.add_argument("--list", action="store_true", help="show what would be recorded and exit (no sound, no recording)")
    args = ap.parse_args(argv)
    unknown = [n for n in args.only if n not in BY_NAME]
    if unknown:
        ap.error(f"unknown clip(s) {unknown}; choose from {', '.join(BY_NAME)}")
    clips = [BY_NAME[n] for n in args.only] if args.only else list(CLIPS)
    devices, notes = present_devices(args.device, input_device_names())
    if args.list:
        list_plan(devices, notes, clips, args.out)
        return 0
    if os.environ.get("NOVA_AUDIO_MUTE", "").strip().lower() in ("1", "true", "yes"):
        print("NOVA_AUDIO_MUTE is set. The recorder plays Nova's reference out loud, so it needs sound: "
              "unset it (env -u NOVA_AUDIO_MUTE ...) and run again.")
        return 2
    for note in notes:
        print(note)
    if not devices:
        print("No capture device found.")
        return 2
    return record(args, devices, clips)


if __name__ == "__main__":
    sys.exit(main())
