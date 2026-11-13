"""Thermal ladder: sheds optional features in order as the SoC heats up, restores them in reverse.

Level 1 caps the avatar at 30 fps, level 2 shortens spoken sentences, level 3 pauses the live
map (the driver loses the map last). Each level releases once the temperature is ``hysteresis`` below its threshold.

``engine.thermal_flags`` holds one boolean per step and True always means the degradation is
active: ``map_service_off`` (stop the map service), ``avatar_30fps`` (cap the avatar at 30 fps),
``short_tts`` (limit sentences to ``engine.short_tts_max_words`` words). All are False when cool.

The ``("thermal", {"level", "temp_c", "throttled", "active"})`` event is emitted on every sample,
not only when the level changes; ``throttled`` is the hex string reported by
``vcgencmd get_throttled`` (``"0x0"`` when unavailable) and ``active`` lists the engaged steps.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("thermal")

STEPS = ("avatar_30fps", "short_tts", "map_service_off")
DEFAULT_LEVELS = (78.0, 81.0, 84.0)
SHORT_TTS_MAX_WORDS = 12
_SYSFS = Path("/sys/class/thermal/thermal_zone0/temp")


def parse_vcgencmd_temp(text: str) -> float:
    m = re.search(r"temp=([-\d.]+)", text)
    if not m:
        raise ValueError("no temperature in vcgencmd output")
    return float(m.group(1))


def parse_throttled(text: str) -> str:
    m = re.search(r"throttled=(0x[0-9a-fA-F]+)", text)
    return m.group(1) if m else "0x0"


def parse_levels(raw: str) -> tuple[float, float, float]:
    try:
        vals = tuple(float(x) for x in raw.split(","))
    except ValueError:
        return DEFAULT_LEVELS
    if len(vals) != 3 or list(vals) != sorted(vals):
        return DEFAULT_LEVELS
    return vals  # type: ignore[return-value]


def _flags() -> dict[str, bool]:
    return dict.fromkeys(STEPS, False)


class ThermalLadder:
    FALLBACK_AFTER = 3
    LOG_EVERY_S = 300.0

    def __init__(self, read_temp: Callable, read_throttled: Callable,
                 apply: Callable[[str, bool], None], *, levels=DEFAULT_LEVELS,
                 hysteresis: float = 3.0, interval: float = 5.0,
                 emit: Callable[[str, object], None] | None = None,
                 fallback_temp: Callable | None = None):
        self.read_temp = read_temp
        self.read_throttled = read_throttled
        self.fallback_temp = fallback_temp
        self.apply = apply
        self.levels = tuple(levels)
        self.hysteresis = hysteresis
        self.interval = interval
        self.emit = emit
        self.level = 0
        self._primary_fails = 0
        self._last_log = float("-inf")

    @property
    def active(self) -> list[str]:
        return list(STEPS[: self.level])

    def update(self, temp: float, throttled: str = "0x0") -> int:
        """Move to the level for ``temp``; ``apply(name, True)`` sheds a step, ``False`` restores it."""
        target = self.level
        while target < len(STEPS) and temp >= self.levels[target]:
            target += 1
        while target > 0 and temp < self.levels[target - 1] - self.hysteresis:
            target -= 1
        while self.level < target:
            self.apply(STEPS[self.level], True)
            self.level += 1
        while self.level > target:
            self.level -= 1
            self.apply(STEPS[self.level], False)
        if self.emit:
            self.emit("thermal", {"level": self.level, "temp_c": temp,
                                  "throttled": throttled, "active": self.active})
        return self.level

    @staticmethod
    def _hex(value) -> str:
        return hex(value) if isinstance(value, int) else str(value)

    def tick(self) -> int:
        temp = self._temp_sync()
        try:
            throttled = self._hex(self.read_throttled())
        except Exception as e:  # noqa: BLE001
            self._warn("throttled read failed: %s", e)
            throttled = "0x0"
        return self.update(temp, throttled)

    def _temp_sync(self) -> float:
        if self.fallback_temp and self._primary_fails >= self.FALLBACK_AFTER:
            return float(self.fallback_temp())
        try:
            temp = float(self.read_temp())
        except Exception:
            self._primary_fails += 1
            if self.fallback_temp:
                return float(self.fallback_temp())
            raise
        self._primary_fails = 0
        return temp

    async def _temp(self) -> float:
        if self.fallback_temp and self._primary_fails >= self.FALLBACK_AFTER:
            return float(await _resolve(self.fallback_temp))
        try:
            temp = float(await _resolve(self.read_temp))
        except Exception:
            self._primary_fails += 1
            if self.fallback_temp:
                return float(await _resolve(self.fallback_temp))
            raise
        self._primary_fails = 0
        return temp

    async def step(self) -> int:
        temp = await self._temp()
        try:
            throttled = self._hex(await _resolve(self.read_throttled))
        except Exception as e:  # noqa: BLE001
            self._warn("throttled read failed: %s", e)
            throttled = "0x0"
        return self.update(temp, throttled)

    def _warn(self, msg: str, *args) -> None:
        now = time.monotonic()
        if now - self._last_log >= self.LOG_EVERY_S:
            self._last_log = now
            log.warning(msg, *args)

    async def run(self) -> None:
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._warn("thermal sample failed: %s", e)
            await asyncio.sleep(self.interval)


async def _resolve(fn: Callable):
    out = fn()
    return await out if inspect.isawaitable(out) else out


async def _vcgencmd(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "vcgencmd", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return out.decode()


def _make_readers():
    """Return (read_temp, read_throttled, fallback_temp) or None when no sensor exists."""
    async def vc_temp() -> float:
        return parse_vcgencmd_temp(await _vcgencmd("measure_temp"))

    async def vc_throttled() -> str:
        return parse_throttled(await _vcgencmd("get_throttled"))

    async def sys_temp() -> float:
        return int(_SYSFS.read_text().strip()) / 1000.0

    async def zero() -> str:
        return "0x0"

    has_sysfs = _SYSFS.exists()
    if shutil.which("vcgencmd"):
        return vc_temp, vc_throttled, (sys_temp if has_sysfs else None)
    if has_sysfs:
        return sys_temp, zero, None
    return None


def _setting(engine, key: str, default: str) -> str:
    cfg = getattr(engine, "cfg", None)
    val = cfg.get(key) if cfg is not None else None
    return str(val or os.environ.get(key) or default)


def install(engine) -> None:
    engine.thermal_flags = _flags()
    engine.short_tts_max_words = SHORT_TTS_MAX_WORDS
    if _setting(engine, "THERMAL", "on").lower() in ("off", "0", "false", "no"):
        log.info("thermal ladder off")
        return
    readers = _make_readers()
    if readers is None:
        log.info("no temperature sensor found; thermal ladder idle")
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("thermal ladder needs a running event loop; idle")
        return

    def apply(name: str, on: bool) -> None:
        engine.thermal_flags = {**engine.thermal_flags, name: on}
        log.info("thermal: %s %s", name, "engaged" if on else "released")

    try:
        hyst = float(_setting(engine, "THERMAL_HYSTERESIS_C", "3"))
        interval = float(_setting(engine, "THERMAL_INTERVAL_S", "5"))
    except ValueError:
        hyst, interval = 3.0, 5.0
    read_temp, read_throttled, fallback = readers
    ladder = ThermalLadder(read_temp, read_throttled, apply,
                           levels=parse_levels(_setting(engine, "THERMAL_LEVELS", "")),
                           hysteresis=hyst, interval=interval, emit=engine.emit,
                           fallback_temp=fallback)
    task = loop.create_task(ladder.run(), name="thermal")

    async def stop() -> None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    closers = getattr(engine, "closers", None)
    if closers is not None:
        closers.append(stop)
    engine.thermal_ladder = ladder
