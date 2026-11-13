"""Voice ID enrollment from the sheet: prompted takes from the engine's microphone, DriveOS quality gate,
``EnrollmentSession`` and ``TemplateStore``, so the template lands where the payment ladder reads it."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from driveos.biometrics.capture import capture_utterance
from driveos.biometrics.enroll import (
    MIN_UTTERANCES,
    PHRASES,
    ConsentRequired,
    NotEnoughSpeech,
)
from driveos.server.enroll import EnrollError

from .consent import ConsentStore
from .mic_tap import MicTap
from .paths import biometrics_config, model_config

log = logging.getLogger("driver.voice")

EXTRA_ATTEMPTS = 4          # re-records allowed on top of the number of good takes wanted
START_TIMEOUT_S = 10.0
MAX_TAKE_S = 8.0
END_SILENCE_MS = 700


class VoiceEnroll:
    def __init__(self, engine, *, home: Path, consent: ConsentStore | None = None, on_change=None, tap=None,
                 session_factory=None, embedder_factory=None, capture=None, on_enrolled=None,
                 total: int = MIN_UTTERANCES):
        self.engine = engine
        self.home = Path(home)
        self.consent = consent or ConsentStore(self.home)
        self.on_change = on_change or (lambda: None)
        self.tap = tap or MicTap()
        self._session_factory = session_factory
        self._embedder_factory = embedder_factory
        self._capture = capture or _capture_take
        self.on_enrolled = on_enrolled
        self.total = max(MIN_UTTERANCES, int(total))
        self._session = None
        self._task: asyncio.Task | None = None
        self._attempts = 0
        self.name = ""
        self.stage = "idle"          # idle | analyzing | ready | recording | done | failed
        self.enrolled = False
        self.step = 0
        self.kept = 0
        self.message = ""
        self.last: dict = {}

    # state -------------------------------------------------------------------
    def state(self) -> dict:
        return {"name": self.name, "stage": self.stage, "enrolled": self.enrolled, "step": self.step,
                "of": self.total, "kept": self.kept, "message": self.message, "last": dict(self.last),
                "done": self.stage == "done", "busy": self.stage in ("analyzing", "recording"),
                "prompt": self._prompt()}

    def _prompt(self) -> str:
        if self.stage not in ("ready", "recording", "analyzing"):
            return ""
        return PHRASES[self._attempts % len(PHRASES)].format(name=self.name)

    def _set(self, **kw) -> None:
        for key, value in kw.items():
            setattr(self, key, value)
        self.engine.emit("enroll", {"kind": "voice", "step": self.step, "of": self.total,
                                    "message": self.message, "done": self.stage == "done"})
        self.on_change()

    def _template_store(self):
        from driveos.biometrics.store import TemplateStore
        return TemplateStore(self.home)

    async def refresh(self) -> None:
        """Whether a template exists and its name, read off the event loop."""
        cfg = biometrics_config(self.home)

        def read():
            store = self._template_store()
            if not store.exists(cfg.driver_id):
                return False, ""
            tpl = store.load(cfg.driver_id)
            return True, (tpl.name if tpl else "")

        enrolled, name = await asyncio.to_thread(read)
        self.enrolled = enrolled
        if enrolled and self.stage in ("idle", "done"):
            self.name = name or self.name

    # session -----------------------------------------------------------------
    async def start(self, name: str) -> None:
        if not self.consent.get("voice"):
            raise ConsentRequired("recording consent is required")
        if self.stage in ("analyzing", "recording"):
            raise EnrollError("a take is in progress")
        clean = (name or "").strip() or "Driver"
        if len(clean) > 32 or not clean.isprintable():
            raise EnrollError("name must be 1-32 printable characters")
        if getattr(self.engine, "state", "idle") != "idle":
            raise EnrollError("Nova is busy; try again in a moment")
        self.name, self._attempts, self.kept, self.step, self.last = clean, 0, 0, 1, {}
        self._set(stage="analyzing", message="Loading the speaker model…")
        try:
            self._session = await (self._session_factory or self._build_session)(clean)
        except EnrollError as e:
            self._set(stage="failed", message=str(e))
            return
        except Exception as e:
            log.exception("could not start voice enrollment")
            self._set(stage="failed", message=str(e) or type(e).__name__)
            return
        self._set(stage="ready", message="Press Record, then read the phrase aloud.")

    async def _build_session(self, name: str):
        from driveos.biometrics.cohort import load_cohort
        from driveos.biometrics.enroll import EnrollmentSession
        from driveos.biometrics.sv import build_embedder

        cfg = biometrics_config(self.home)
        model_cfg = model_config(self.home)
        embedder = await asyncio.to_thread(self._embedder_factory or build_embedder, model_cfg)
        if embedder is None:
            raise EnrollError("speaker model missing (run: driveos fetch-models)")
        cohort = await asyncio.to_thread(load_cohort, model_cfg.models_dir, embedder.name)
        return EnrollmentSession(embedder, self._template_store(), driver_id=cfg.driver_id, name=name,
                                 consent=True, cohort=cohort)

    def take(self) -> asyncio.Task:
        """Start a take in the background; the sheet is updated through ``on_change`` as it progresses."""
        if self._session is None or self.stage != "ready":
            raise EnrollError("not ready to record; press Start first")
        if getattr(self.engine, "state", "idle") != "idle":
            raise EnrollError("Nova is busy; try again in a moment")
        if not self.tap.attach(getattr(self.engine, "mic", None)):
            raise EnrollError("The microphone is unavailable.")
        self._set(stage="recording", message="Listening… read the phrase aloud.")
        self._task = asyncio.ensure_future(self._take())
        return self._task

    async def _take(self) -> None:
        session = self._session
        try:
            with self.tap.capturing():
                cap = await self._capture(self.tap)
            if not cap.voiced:
                self._set(stage="ready", message="I didn't hear you. Check the microphone and try again.")
                return
            self._set(stage="analyzing", message="Checking the recording…")
            report = await asyncio.to_thread(session.add, cap.ch0)
            self._attempts += 1
            q = report.quality
            self.last = {"ok": bool(report.accepted), "voiced_s": round(q.voiced_s, 1),
                         "snr_db": round(q.snr_db, 1), "clipping_pct": round(q.clip_frac * 100, 1),
                         "reasons": list(q.reasons)}
            good = len(session.accepted)
            self.kept = good
            if not report.accepted:
                why = "; ".join(q.reasons) or "the take was not usable"
                if self._attempts >= self.total + EXTRA_ATTEMPTS:
                    await self._finish(session)
                    return
                self._set(stage="ready", message=f"That take didn't pass: {why}. Try again.")
                return
            if good >= self.total or self._attempts >= self.total + EXTRA_ATTEMPTS:
                await self._finish(session)
                return
            self._set(stage="ready", step=min(good + 1, self.total),
                      message=f"Good. Take {good + 1} of {self.total}.")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("voice enrollment take failed")
            self._set(stage="failed", message=str(e) or type(e).__name__)

    async def _finish(self, session) -> None:
        try:
            res = await asyncio.to_thread(session.finalize)
        except NotEnoughSpeech as e:
            self._set(stage="failed", message=f"{e}. Press Start to try again.")
            return
        if self.on_enrolled is not None:
            try:
                out = self.on_enrolled()
                if asyncio.iscoroutine(out):
                    await out
            except Exception:
                log.exception("could not switch payments to Voice ID")
        self._session = None
        self._set(stage="done", enrolled=True, kept=res.kept, step=self.total, message="Voice ID saved.")

    # edits -------------------------------------------------------------------
    async def rename(self, name: str) -> None:
        clean = (name or "").strip()
        if not clean or len(clean) > 32 or not clean.isprintable():
            raise EnrollError("name must be 1-32 printable characters")
        cfg = biometrics_config(self.home)

        def write():
            store = self._template_store()
            tpl = store.load(cfg.driver_id)
            if tpl is None:
                return False
            tpl.name = clean
            store.save(tpl)
            return True

        if not await asyncio.to_thread(write):
            raise EnrollError("no Voice ID to rename")
        self._set(name=clean, message=f"Voice ID is now {clean}.")

    async def delete(self) -> None:
        cfg = biometrics_config(self.home)
        await asyncio.to_thread(self._template_store().delete, cfg.driver_id)
        self._session = None
        self._attempts = 0
        self._set(stage="idle", enrolled=False, kept=0, step=0, last={}, message="Voice ID removed.")

    async def cancel(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
        self.tap.detach()
        self._session = None
        if self.stage != "done":
            self._set(stage="idle", step=0, kept=0, message="")


async def _capture_take(ring):
    return await capture_utterance(ring, start_timeout_s=START_TIMEOUT_S, max_s=MAX_TAKE_S,
                                   end_silence_ms=END_SILENCE_MS)
