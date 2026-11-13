"""Face ID profiles through DriveOS's F455 enrollment service. A missing camera is degraded, not a crash."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

from driveos.biometrics.enroll import ConsentRequired
from driveos.server.enroll import FaceEnrollService

from .consent import ConsentStore
from .paths import biometrics_config

log = logging.getLogger("driver.face")

NO_CAMERA = "No face camera on this device."
STAGE_TEXT = {
    "capturing": "Look at the camera and keep still.",
    "deleting": "Deleting the face profile…",
    "done": "Face profile saved.",
}


class _Host:
    """The slice of the DriveOS agent the face service reads: no voice capture, and the cabin's checkout state."""
    bio_capture = False

    def __init__(self, engine):
        self._engine = engine

    @property
    def shop(self):
        payment = getattr(self._engine, "payment", None)
        return SimpleNamespace(checkout=getattr(payment, "co", None))


class FaceEnroll:
    def __init__(self, engine, *, home: Path, consent: ConsentStore | None = None, on_change=None, client=None,
                 voice_active=None):
        self.engine = engine
        self.home = Path(home)
        self.consent = consent or ConsentStore(self.home)
        self.on_change = on_change or (lambda: None)
        self._client = client
        self._voice_active = voice_active
        self._service: FaceEnrollService | None = None
        self._tasks: set[asyncio.Task] = set()
        self.degraded = False
        self.notice = ""
        self.profiles: list[dict] = []
        self.selected = ""
        self.max_profiles = 5
        self._loop: asyncio.AbstractEventLoop | None = None

    # service -----------------------------------------------------------------
    def _svc(self) -> FaceEnrollService:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        if self._service is None:
            cfg = biometrics_config(self.home)
            if self._client is None:
                from driveos.biometrics.f455 import F455Client
                self._client = F455Client(cfg.f455_lib)
            self._service = FaceEnrollService(
                SimpleNamespace(biometrics=cfg), _Host(self.engine), on_change=self._changed,
                client=self._client, voice_active=self._voice_active)
        return self._service

    def _changed(self) -> None:
        """Called by the service on every stage change, on the loop or in a worker thread; re-reads the stores
        off the loop."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._schedule_sync)
            return
        self._schedule_sync()

    def _schedule_sync(self) -> None:
        task = asyncio.ensure_future(self._sync())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _sync(self) -> None:
        try:
            await self._read_profiles()
        except Exception:
            log.exception("could not read face profiles")
        self._emit()
        self.on_change()

    async def _read_profiles(self) -> None:
        data = await asyncio.to_thread(self._svc().profiles_state)
        self.profiles = [dict(p) for p in data["items"]]
        self.selected = data["selected_id"] or ""
        self.max_profiles = int(data.get("max", 5))

    async def refresh(self) -> None:
        """Probe the camera and read the profile list, both off the event loop."""
        svc = self._svc()
        present = await asyncio.to_thread(svc.client.device_present)
        self.degraded = not present
        if self.degraded:
            self.notice = NO_CAMERA
        elif self.notice == NO_CAMERA:
            self.notice = ""
        try:
            await self._read_profiles()
        except (ValueError, OSError) as e:
            self.notice = str(e)

    # state -------------------------------------------------------------------
    def _stage(self) -> dict:
        st = self._svc().state() if self._service is not None else None
        return st or {}

    def state(self) -> dict:
        st = self._stage()
        stage = st.get("stage", "")
        if self.degraded and stage not in ("capturing", "link_confirm", "deleting"):
            message = NO_CAMERA
        elif stage == "failed":
            message = st.get("error") or "Face enrollment failed."
        elif stage == "link_confirm":
            message = "This face is already on the camera. Link it to this driver?"
        elif stage in STAGE_TEXT:
            message = STAGE_TEXT[stage]
        else:
            message = self.notice
        return {"degraded": self.degraded, "message": message, "stage": stage,
                "busy": stage in ("capturing", "deleting"), "link": stage == "link_confirm",
                "name": st.get("name", ""), "selected": self.selected, "profiles": list(self.profiles),
                "max": self.max_profiles, "full": len(self.profiles) >= self.max_profiles}

    def _emit(self) -> None:
        st = self.state()
        self.engine.emit("enroll", {"kind": "face", "step": 0 if st["degraded"] else 1, "of": 1,
                                    "message": st["message"], "done": st["stage"] == "done",
                                    "degraded": st["degraded"]})

    # commands ----------------------------------------------------------------
    async def start(self, name: str, profile_id: str | None = None) -> None:
        """Enroll (``profile_id`` "new" or None) or re-enroll an existing profile."""
        if not self.consent.get("face"):
            raise ConsentRequired("face enrollment consent is required")
        svc = self._svc()
        if not await asyncio.to_thread(svc.client.device_present):
            self.degraded = True
            self.notice = NO_CAMERA
            self._emit()
            self.on_change()
            return
        self.degraded = False
        self.notice = ""
        svc.start(name, True, profile_id or "new")

    async def select(self, profile_id: str) -> None:
        await asyncio.to_thread(self._svc().select, profile_id)
        self.notice = "Face profile selected."

    async def rename(self, profile_id: str, name: str) -> None:
        await asyncio.to_thread(self._svc().edit, profile_id, name, None)
        self.notice = "Face profile renamed."

    async def link(self) -> None:
        self._svc().link(True)
        getattr(getattr(self.engine, "payment", None), "refresh_verifiers", lambda: None)()

    async def delete(self, profile_id: str) -> None:
        self._svc().delete(True, profile_id)

    def dismiss(self) -> None:
        """Cancel a running capture or an unconfirmed link, or clear a finished or failed operation."""
        if self._service is not None:
            self._service.cancel()
