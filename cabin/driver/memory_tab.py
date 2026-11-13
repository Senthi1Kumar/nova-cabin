"""What Nova remembers: the engine's profile store, listed field by field with forget and forget-all."""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("driver.memory")

LABELS = {"name": "Name", "home_city": "Home city", "work_city": "Work city",
          "preferred_places": "Favourite places", "usual_orders": "Usual orders", "interests": "Interests",
          "units": "Units", "temp_pref_c": "Cabin temperature"}


def _show(key: str, value) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if key == "temp_pref_c":
        return f"{value:g} °C"
    return str(value)


class MemoryTab:
    def __init__(self, engine):
        self.engine = engine
        self._fields: list[dict] = []
        self._line = ""
        self._available = False

    def _memory(self):
        mem = getattr(self.engine, "memory", None)
        return mem if mem is not None and getattr(mem, "profile", None) is not None else None

    async def refresh(self) -> None:
        mem = self._memory()
        if mem is None:
            self._available, self._fields, self._line = False, [], ""
            return
        try:
            profile = await asyncio.to_thread(mem.profile.load)
        except Exception:
            log.exception("profile unavailable")
            self._available, self._fields, self._line = False, [], ""
            return
        from ..memory.profile import profile_line
        self._available = True
        self._line = profile_line(profile) or ""
        self._fields = [
            {"field": key, "label": label, "value": _show(key, getattr(profile, key, None))}
            for key, label in LABELS.items()
            if getattr(profile, key, None) not in (None, "", [], {})]

    def snapshot(self) -> dict:
        return {"available": self._available, "line": self._line, "fields": list(self._fields)}

    async def forget(self, field: str) -> None:
        mem = self._memory()
        if mem is None or field not in LABELS:
            return
        await asyncio.to_thread(mem.profile.forget, field)
        await mem._refresh()
        await self.refresh()

    async def forget_all(self) -> None:
        mem = self._memory()
        if mem is None:
            return
        await asyncio.to_thread(mem.profile.forget_all)
        await mem.purge()
        await mem._refresh()
        await self.refresh()
