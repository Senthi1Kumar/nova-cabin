"""Text-to-speech providers.

    audio = await tts.synth(text)     # -> Speech(pcm16 @ 24 kHz, word timings or None)

ElevenLabs returns character timings (best lip sync). Others return audio
only; word timings are then estimated from text over the real duration.
`system` uses the OS voice (macOS `say`, Linux `espeak-ng`) so the app
speaks with no key at all.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import shutil
import sys
import tempfile
import wave
from dataclasses import dataclass

import httpx
import numpy as np

from .audio import TTS_SR, resample
from .config import Settings

log = logging.getLogger("tts")


@dataclass
class Speech:
    pcm16: bytes
    words: list[tuple[str, float, float]]      # (word, start_s, dur_s) relative to chunk

    @property
    def duration(self) -> float:
        return len(self.pcm16) / 2 / TTS_SR


def estimate_words(text: str, duration: float) -> list[tuple[str, float, float]]:
    words = text.split()
    if not words:
        return []
    lead, tail = 0.05, 0.12
    span = max(duration - lead - tail, 0.1)
    weights = [len(w) + 2.5 + (3 if w.endswith((",", ".", "!", "?", "—", ";", ":")) else 0) for w in words]
    total = sum(weights)
    out, t = [], lead
    for w, k in zip(words, weights):
        d = span * k / total
        out.append((w, t, d))
        t += d
    return out


_CLAUSE_BREAK = re.compile(r"\s*[\u2014;\u2026]\s*|\s+-\s+|\s*:(?!\d)\s*")


def split_clauses(text: str, pause_ms: int = 220) -> list[tuple[str, int]]:
    """Split at em dashes, spaced hyphens, semicolons, ellipses and colons (not the colon in 10:30).

    Each clause but the last carries ``pause_ms`` of silence to play after it; the delimiter itself is dropped.
    """
    out: list[tuple[str, int]] = []
    pos = 0
    for m in _CLAUSE_BREAK.finditer(text):
        part = text[pos:m.start()].strip()
        pos = m.end()
        if part:
            out.append((part, pause_ms))
    tail = text[pos:].strip()
    if tail:
        out.append((tail, 0))
    elif out:
        out[-1] = (out[-1][0], 0)
    return out


def words_from_chars(chars: list[str], starts: list[float], ends: list[float]) -> list[tuple[str, float, float]]:
    out, cur, t0, t1 = [], "", None, 0.0
    for c, s, e in zip(chars, starts, ends):
        if c.isspace():
            if cur:
                out.append((cur, t0, max(t1 - t0, 0.05)))
            cur, t0 = "", None
            continue
        if t0 is None:
            t0 = s
        cur += c
        t1 = e
    if cur:
        out.append((cur, t0, max(t1 - t0, 0.05)))
    return out


class OpenAITTS:
    label = "OpenAI"

    def __init__(self, http, base, key, model, voice, instructions) -> None:
        self.http, self.base, self.key = http, base, key
        self.model, self.voice, self.instructions = model, voice, instructions

    async def synth(self, text: str) -> Speech:
        body = {"model": self.model, "voice": self.voice, "input": text, "response_format": "pcm"}
        if self.instructions and "tts-1" not in self.model:
            body["instructions"] = self.instructions
        r = await self.http.post(f"{self.base}/audio/speech", json=body,
                                 headers={"Authorization": f"Bearer {self.key}"})
        r.raise_for_status()
        pcm = r.content
        return Speech(pcm, estimate_words(text, len(pcm) / 2 / TTS_SR))


class ElevenLabsTTS:
    label = "ElevenLabs"

    def __init__(self, http, key, voice, model) -> None:
        self.http, self.key, self.voice, self.model = http, key, voice, model

    async def synth(self, text: str) -> Speech:
        r = await self.http.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice}/with-timestamps",
            params={"output_format": "pcm_24000"},
            headers={"xi-api-key": self.key},
            json={"text": text, "model_id": self.model},
        )
        r.raise_for_status()
        d = r.json()
        pcm = base64.b64decode(d["audio_base64"])
        al = d.get("alignment") or d.get("normalized_alignment")
        words = (words_from_chars(al["characters"], al["character_start_times_seconds"],
                                  al["character_end_times_seconds"]) if al else
                 estimate_words(text, len(pcm) / 2 / TTS_SR))
        return Speech(pcm, words)


class DeepgramTTS:
    label = "Deepgram"

    def __init__(self, http, key, model) -> None:
        self.http, self.key, self.model = http, key, model

    async def synth(self, text: str) -> Speech:
        r = await self.http.post(
            "https://api.deepgram.com/v1/speak",
            params={"model": self.model, "encoding": "linear16", "sample_rate": TTS_SR, "container": "none"},
            headers={"Authorization": f"Token {self.key}"},
            json={"text": text},
        )
        r.raise_for_status()
        pcm = r.content
        return Speech(pcm, estimate_words(text, len(pcm) / 2 / TTS_SR))


class SystemTTS:
    """OS voice. macOS: `say` (pick a voice with SYSTEM_TTS_VOICE, e.g. Samantha)."""
    label = "System voice"

    def __init__(self, voice: str = "") -> None:
        self.voice = voice
        self.cmd = "say" if sys.platform == "darwin" and shutil.which("say") else \
                   (shutil.which("espeak-ng") or shutil.which("espeak"))

    @property
    def available(self) -> bool:
        return bool(self.cmd)

    async def synth(self, text: str) -> Speech:
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            if self.cmd == "say":
                args = ["say", "-o", path, "--file-format=WAVE", "--data-format=LEI16@24000"]
                if self.voice:
                    args += ["-v", self.voice]
                args.append(text)
            else:
                args = [self.cmd, "-w", path] + (["-v", self.voice] if self.voice else []) + [text]
            p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                     stderr=asyncio.subprocess.PIPE)
            _, err = await p.communicate()
            if p.returncode:
                raise RuntimeError(err.decode(errors="ignore")[:200])
            with wave.open(path, "rb") as w:
                sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
                raw = w.readframes(w.getnframes())
            x = np.frombuffer(raw, np.int16 if sw == 2 else np.uint8).astype(np.float32)
            x = x / 32768.0 if sw == 2 else (x - 128) / 128.0
            if ch > 1:
                x = x.reshape(-1, ch).mean(1)
            pcm = (np.clip(resample(x, sr, TTS_SR), -1, 1) * 32767).astype(np.int16).tobytes()
            return Speech(pcm, estimate_words(text, len(pcm) / 2 / TTS_SR))
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


def _phonon(cfg: Settings, require_cached: bool):
    try:
        from .providers.tts_phonon import PhononTTS, model_cached
        if require_cached and not model_cached():
            return None
        return PhononTTS(voice=cfg.get("PHONON_VOICE") or None,
                         duration_delta=cfg.num("PHONON_DURATION_DELTA", -0.25))
    except Exception as e:                               # noqa: BLE001
        log.info("Phonon unavailable: %s", e)
        return None


def make_tts(cfg: Settings, http: httpx.AsyncClient):
    pref = cfg.get("TTS_PROVIDER", "auto").lower()
    cands = {
        "phonon": lambda: _phonon(cfg, require_cached=pref == "auto"),
        "elevenlabs": lambda: cfg.get("ELEVENLABS_API_KEY") and ElevenLabsTTS(
            http, cfg.get("ELEVENLABS_API_KEY"), cfg.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
            cfg.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")),
        "openai": lambda: cfg.get("OPENAI_API_KEY") and OpenAITTS(
            http, cfg.get("OPENAI_BASE_URL", "https://api.openai.com/v1"), cfg.get("OPENAI_API_KEY"),
            cfg.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"), cfg.get("OPENAI_TTS_VOICE", "coral"),
            cfg.get("OPENAI_TTS_INSTRUCTIONS")),
        "deepgram": lambda: cfg.get("DEEPGRAM_API_KEY") and DeepgramTTS(
            http, cfg.get("DEEPGRAM_API_KEY"), cfg.get("DEEPGRAM_TTS_MODEL", "aura-2-thalia-en")),
        "system": lambda: (lambda s: s if s.available else None)(SystemTTS(cfg.get("SYSTEM_TTS_VOICE"))),
    }
    order = [pref] if pref in cands else []
    order += [k for k in ("phonon", "elevenlabs", "openai", "deepgram", "system") if k not in order]
    for k in order:
        p = cands[k]()
        if p:
            return p
    return None
