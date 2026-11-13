"""Resource sampler: CPU per core, busy threads, memory, Pi thermal/clock/throttle, Hailo NPU.

Every probe degrades to ``None`` when its source is absent (no ``vcgencmd`` off the Pi, no HailoRT
monitor without a running model). psutil is used when importable, ``/proc`` otherwise.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError:  # /proc fallback
    psutil = None

log = logging.getLogger("telemetry")
_TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PROBE_TIMEOUT_S = 2.0
_AEC_KEYS = ("aec", "aec_impl", "erle_db", "residual_db", "mode")


def _run(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _proc_stat() -> list[tuple[int, int]]:
    """(busy, total) jiffies per core from /proc/stat."""
    rows = []
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if re.match(r"cpu\d+ ", line):
                v = [int(x) for x in line.split()[1:]]
                idle = v[3] + (v[4] if len(v) > 4 else 0)
                rows.append((sum(v[:8]) - idle, sum(v[:8])))
    except (OSError, ValueError):
        pass
    return rows


def _thread_times() -> dict[str, float]:
    """CPU seconds per thread of this process."""
    out: dict[str, float] = {}
    if psutil is not None:
        try:
            for t in psutil.Process().threads():
                out[str(t.id)] = t.user_time + t.system_time
            return out
        except Exception:  # noqa: BLE001
            return out
    try:
        for p in Path("/proc/self/task").iterdir():
            f = (p / "stat").read_text().rsplit(")", 1)[1].split()
            out[p.name] = (int(f[11]) + int(f[12])) / _TICK
    except (OSError, ValueError, IndexError):
        pass
    return out


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _rss_mb() -> float | None:
    try:
        if psutil is not None:
            return psutil.Process().memory_info().rss / 1048576
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError):
        pass
    return None


class Sampler:
    def __init__(self, interval_s: float = 5, engine: Any = None, telemetry: Any = None,
                 emit: Callable[[str, Any], None] | None = None,
                 run: Callable[[list[str]], str | None] = _run, sys_root: Path | str = "/sys") -> None:
        self._sys = Path(sys_root)
        self._npu: str | None = None
        self.interval_s = interval_s
        self.engine = engine
        self.telemetry = telemetry
        self.emit = emit
        self._run = run
        self._cpu = _proc_stat() if psutil is None else None
        self._threads = _thread_times()
        self._t = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if psutil is not None:
            psutil.cpu_percent(percpu=True)

    # ── probes ──
    def _cpu_per_core(self) -> list[float]:
        if psutil is not None:
            return [round(x, 1) for x in psutil.cpu_percent(percpu=True)]
        now = _proc_stat()
        prev, self._cpu = self._cpu or [], now
        out = []
        for (b1, t1), (b0, t0) in zip(now, prev):
            out.append(round(100.0 * (b1 - b0) / (t1 - t0), 1) if t1 > t0 else 0.0)
        return out

    def _busy_threads(self, dt: float) -> int:
        now, prev = _thread_times(), self._threads
        self._threads = now
        return sum(1 for k, v in now.items() if dt > 0 and (v - prev.get(k, v)) / dt > 0.1)

    def _vc(self, *args: str) -> str | None:
        return self._run(["vcgencmd", *args])

    def _temp_c(self) -> float | None:
        raw = _read(self._sys / "class/thermal/thermal_zone0/temp")
        return round(int(raw) / 1000, 1) if raw and raw.lstrip("-").isdigit() else None

    def _arm_mhz(self) -> float | None:
        raw = _read(self._sys / "devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
        return round(int(raw) / 1000, 1) if raw and raw.isdigit() else None

    def _throttled(self) -> bool | None:
        m = re.search(r"throttled=(0x[0-9a-fA-F]+)", self._vc("get_throttled") or "")
        return bool(int(m.group(1), 16) & 0xF) if m else None

    def _npu_state(self) -> str:
        """"present" / "absent" from a one-shot ``hailortcli fw-control identify``, probed once."""
        if self._npu is None:
            self._npu = "present" if "Device Architecture" in (self._run(["hailortcli", "fw-control", "identify"])
                                                              or "") else "absent"
        return self._npu

    def _fps(self) -> float | None:
        eng = self.engine
        fps, ts = getattr(eng, "ui_fps", None), getattr(eng, "ui_fps_ts", 0.0)
        return fps if fps is not None and time.monotonic() - ts < 3.0 else None

    # ── sample ──
    def sample(self) -> dict[str, Any]:
        now = time.monotonic()
        dt, self._t = now - self._t, now
        eng = self.engine
        rss = _rss_mb()
        rec: dict[str, Any] = {
            "cpu_per_core": self._cpu_per_core(),
            "threads_busy": self._busy_threads(dt),
            "rss_mb": rss and round(rss, 1),
            "temp_c": self._temp_c(),
            "arm_mhz": self._arm_mhz(),
            "throttled": self._throttled(),
            "npu": self._npu_state(),
            "npu_util": None,
            "fps": self._fps(),
        }
        flags = getattr(eng, "thermal_flags", None)
        if flags is not None:
            rec["thermal_flags"] = flags
        stats = getattr(getattr(getattr(eng, "vad", None), "_frontend", None), "stats", None)
        if stats:
            stats = stats() if callable(stats) else stats
            rec["aec"] = {k: stats.get(k) for k in _AEC_KEYS} if isinstance(stats, dict) else None
        return rec

    def diagnostics(self, rec: dict[str, Any]) -> dict[str, Any]:
        eng, tel = self.engine, self.telemetry
        cores = rec.get("cpu_per_core") or []
        turn = getattr(tel, "_turn", None)
        try:
            last = turn.record() if turn is not None else {}
        except Exception:  # noqa: BLE001
            last = {}
        veh = getattr(eng, "vehicle", None)
        aec = rec.get("aec") or {}
        return {
            "temp_c": rec["temp_c"], "throttled": rec["throttled"], "arm_mhz": rec["arm_mhz"],
            "cpu": round(sum(cores) / len(cores), 1) if cores else None,
            "cpu_per_core": cores, "rss_mb": rec["rss_mb"], "fps": rec["fps"], "npu": rec["npu"], "npu_util": rec["npu_util"],
            "latency": {k: last.get(k) for k in ("stt_ms", "first_token_ms", "tools_ms", "tts_first_ms",
                                                 "first_audio_ms", "total_ms", "ttfa_ms")},
            "devices": {
                "mic": "sim" if getattr(eng, "sim", False) else "live",
                "aec": aec.get("mode") or ("on" if aec.get("aec") else None),
                "erle_db": aec.get("erle_db"),
                "gps": getattr(veh, "fix_state", None),
                "thermal": rec.get("thermal_flags"),
            },
            "telemetry": tel.status() if tel is not None else {"enabled": False},
        }

    def tick(self) -> dict[str, Any]:
        rec = self.sample()
        if self.telemetry is not None:
            self.telemetry.perf(**rec)
        if self.emit is not None:
            self.emit("diagnostics", self.diagnostics(rec))
        return rec

    # ── lifecycle ──
    def start(self) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            self._npu_state()
            while not self._stop.wait(self.interval_s):
                try:
                    self.tick()
                except Exception:
                    log.debug("sample failed", exc_info=True)
        self._thread = threading.Thread(target=loop, name="telemetry-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
