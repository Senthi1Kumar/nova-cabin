"""Speech-to-text providers.

All providers implement the same session API so the engine never branches:

    s = provider.session(on_partial)
    await s.feed(pcm16_bytes)      # 16 kHz mono, called per 20 ms frame
    text = await s.finish()        # final transcript ("" if nothing heard)
    await s.cancel()

Deepgram streams over a websocket and yields live partials; the REST
providers (OpenAI, Groq, Deepgram prerecorded) transcribe on finish().
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import httpx

from .audio import MIC_SR, wav_bytes
from .config import Settings

log = logging.getLogger("stt")
Partial = Callable[[str], Awaitable[None] | None]


class _BufferedSession:
    def __init__(self, transcribe) -> None:
        self._pcm = bytearray()
        self._transcribe = transcribe

    async def feed(self, pcm: bytes) -> None:
        self._pcm.extend(pcm)

    async def finish(self) -> str:
        if len(self._pcm) < MIC_SR * 2 * 0.25:           # < 250 ms
            return ""
        return (await self._transcribe(bytes(self._pcm))).strip()

    async def cancel(self) -> None:
        self._pcm.clear()


class OpenAICompatSTT:
    """OpenAI /audio/transcriptions (also Groq)."""

    def __init__(self, http: httpx.AsyncClient, base: str, key: str, model: str, label: str, lang: str) -> None:
        self.http, self.base, self.key, self.model, self.label, self.lang = http, base, key, model, label, lang

    def session(self, _on_partial: Partial | None = None):
        return _BufferedSession(self._run)

    async def _run(self, pcm: bytes) -> str:
        r = await self.http.post(
            f"{self.base}/audio/transcriptions",
            headers={"Authorization": f"Bearer {self.key}"},
            data={"model": self.model, "language": self.lang, "response_format": "json"},
            files={"file": ("speech.wav", wav_bytes(pcm), "audio/wav")},
        )
        r.raise_for_status()
        return r.json().get("text", "")


class DeepgramSTT:
    def __init__(self, http: httpx.AsyncClient, key: str, model: str, lang: str) -> None:
        self.http, self.key, self.model, self.lang = http, key, model, lang
        self.label = "Deepgram"

    def session(self, on_partial: Partial | None = None):
        try:
            import websockets  # noqa: F401
            return _DeepgramLive(self, on_partial)
        except ImportError:
            return _BufferedSession(self._rest)

    async def _rest(self, pcm: bytes) -> str:
        r = await self.http.post(
            "https://api.deepgram.com/v1/listen",
            params={"model": self.model, "smart_format": "true", "language": self.lang},
            headers={"Authorization": f"Token {self.key}", "Content-Type": "audio/wav"},
            content=wav_bytes(pcm),
        )
        r.raise_for_status()
        return r.json()["results"]["channels"][0]["alternatives"][0]["transcript"]


class _DeepgramLive:
    URL = "wss://api.deepgram.com/v1/listen"

    def __init__(self, dg: DeepgramSTT, on_partial: Partial | None) -> None:
        self.dg = dg
        self.on_partial = on_partial
        self.finals: list[str] = []
        self.interim = ""
        self._ws = None
        self._pending: list[bytes] = []
        self._rx: asyncio.Task | None = None
        self._connect = asyncio.create_task(self._open())
        self._closed = asyncio.Event()
        self._fallback = bytearray()     # if the socket fails we still transcribe via REST

    async def _open(self) -> None:
        import websockets
        q = (f"?model={self.dg.model}&language={self.dg.lang}&encoding=linear16&sample_rate={MIC_SR}"
             "&channels=1&interim_results=true&smart_format=true&punctuate=true")
        hdr = {"Authorization": f"Token {self.dg.key}"}
        try:
            try:
                self._ws = await websockets.connect(self.URL + q, additional_headers=hdr, open_timeout=5)
            except TypeError:                                      # websockets < 14
                self._ws = await websockets.connect(self.URL + q, extra_headers=hdr, open_timeout=5)
            for p in self._pending:
                await self._ws.send(p)
            self._pending.clear()
            self._rx = asyncio.create_task(self._recv())
        except Exception as e:                                     # noqa: BLE001
            log.warning("deepgram live unavailable, using REST: %s", e)
            self._ws = None
            self._closed.set()

    async def _recv(self) -> None:
        try:
            async for msg in self._ws:
                d = json.loads(msg)
                if d.get("type") != "Results":
                    continue
                alt = d["channel"]["alternatives"][0]["transcript"]
                if d.get("is_final"):
                    if alt:
                        self.finals.append(alt)
                    self.interim = ""
                else:
                    self.interim = alt
                text = " ".join(self.finals + ([self.interim] if self.interim else []))
                if self.on_partial and text:
                    res = self.on_partial(text)
                    if asyncio.iscoroutine(res):
                        await res
        except Exception:                                          # noqa: BLE001
            pass
        finally:
            self._closed.set()

    async def feed(self, pcm: bytes) -> None:
        self._fallback.extend(pcm)
        if self._ws is not None:
            try:
                await self._ws.send(pcm)
            except Exception:                                      # noqa: BLE001
                pass
        elif not self._connect.done():
            self._pending.append(pcm)

    async def finish(self) -> str:
        await self._connect
        if self._ws is None:
            return (await self.dg._rest(bytes(self._fallback))).strip() if len(self._fallback) > 8000 else ""
        try:
            await self._ws.send(json.dumps({"type": "CloseStream"}))
            await asyncio.wait_for(self._closed.wait(), 3.0)
        except Exception:                                          # noqa: BLE001
            pass
        await self._close()
        text = " ".join(self.finals).strip()
        return text or self.interim.strip()

    async def cancel(self) -> None:
        if not self._connect.done():
            self._connect.cancel()
        await self._close()

    async def _close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:                                      # noqa: BLE001
                pass
        if self._rx:
            self._rx.cancel()


def make_stt(cfg: Settings, http: httpx.AsyncClient):
    pref = cfg.get("STT_PROVIDER", "auto").lower()
    lang = cfg.get("LOCALE", "en-US").split("-")[0]
    cands = {
        "deepgram": lambda: cfg.get("DEEPGRAM_API_KEY") and DeepgramSTT(
            http, cfg.get("DEEPGRAM_API_KEY"), cfg.get("DEEPGRAM_STT_MODEL", "nova-3"), lang),
        "openai": lambda: cfg.get("OPENAI_API_KEY") and OpenAICompatSTT(
            http, cfg.get("OPENAI_BASE_URL", "https://api.openai.com/v1"), cfg.get("OPENAI_API_KEY"),
            cfg.get("OPENAI_STT_MODEL", "gpt-4o-mini-transcribe"), "OpenAI", lang),
        "groq": lambda: cfg.get("GROQ_API_KEY") and OpenAICompatSTT(
            http, "https://api.groq.com/openai/v1", cfg.get("GROQ_API_KEY"),
            cfg.get("GROQ_STT_MODEL", "whisper-large-v3-turbo"), "Groq", lang),
    }
    def parakeet():
        from .providers.stt_parakeet import load
        return load(cfg.get("PARAKEET_LIB"), cfg.get("PARAKEET_MODEL"), cfg.flag("STT_PARTIALS"), cfg.flag("ASR_GATE", True))
    cands["parakeet"] = parakeet
    order = [pref] if pref in cands else []
    order += [k for k in ("parakeet", "deepgram", "openai", "groq") if k not in order]
    for k in order:
        p = cands[k]()
        if p:
            return p
    return None
