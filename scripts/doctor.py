#!/usr/bin/env python3
"""Nova Cabin doctor: one table of every peripheral, model and key with its state and the fix.

    python scripts/doctor.py [--json]

Exit status is 0 when no required row is "missing", 1 otherwise. Every probe has a 5 s budget and cannot
crash the report. Keys are reported as set or not set, never printed.
"""
from __future__ import annotations

import argparse
import glob
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

State = Literal["ok", "degraded", "missing"]
PROBE_TIMEOUT_S = 5.0
F455_TARGET_OPFW = "9.16.1.1"
GPS_PORT = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"
KEYS = (("OPENROUTER_API_KEY", True), ("MAPBOX_ACCESS_TOKEN", True), ("EXA_API_KEY", False),
        ("TYPESAFE_API_KEY", False))
WEBCAM_RE = re.compile(r"c9\d\d|webcam|logitech|uvc camera", re.IGNORECASE)


@dataclass
class Check:
    name: str
    state: State
    detail: str
    fix: str = ""
    required: bool = False


@dataclass
class Ctx:
    """Everything a probe touches outside this process, so tests can substitute it."""
    run: Callable[[list[str], float], str | None] = field(default=None)  # type: ignore[assignment]
    read: Callable[[str], str | None] = field(default=None)  # type: ignore[assignment]
    glob: Callable[[str], list[str]] = field(default=glob.glob)
    env: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    head: Callable[[str, float], tuple[bool, str]] = field(default=None)  # type: ignore[assignment]
    devices: Callable[[str], tuple[list[tuple[int, str]], str | None]] = field(default=None)  # type: ignore[assignment]
    settings: object = None
    home: Path = field(default_factory=Path.home)

    def __post_init__(self) -> None:
        self.run = self.run or _run
        self.read = self.read or _read
        self.head = self.head or _head
        self.devices = self.devices or _devices
        if self.settings is None:
            from cabin.config import Settings
            self.settings = Settings.load()

    def cfg(self, key: str, default: str = "") -> str:
        return self.settings.get(key, default)  # type: ignore[attr-defined]


def _run(cmd: list[str], timeout: float, stdin: str | None = None) -> str | None:
    """Combined output of a command, or None when it is absent or fails to start."""
    if shutil.which(cmd[0]) is None and not os.path.isabs(cmd[0]):
        return None
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return (p.stdout or "") + (p.stderr or "") if p.returncode == 0 or p.stdout else None


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return None


def _head(url: str, timeout: float) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return True, f"HTTP {e.code}"                     # the host answered
    except Exception as e:                               # noqa: BLE001
        return False, type(e).__name__


# ── probes ─────────────────────────────────────────────────────────────────────────────────────────────────

def _alsa_cards(ctx: Ctx) -> str:
    return (ctx.read("/proc/asound/cards") or "").lower()


def _devices(kind: str) -> tuple[list[tuple[int, str]], str | None]:
    """Usable PortAudio devices of one kind and the name of the system default (None when unknown)."""
    sd = importlib.import_module("cabin.audio").sd
    if sd is None:
        return [], None
    devs = [(i, str(d["name"])) for i, d in enumerate(sd.query_devices()) if d.get(f"max_{kind}_channels", 0) > 0]
    try:
        default = str(sd.query_devices(kind=kind)["name"])
    except Exception:                                    # noqa: BLE001
        default = None
    return devs, default


def _is_raw_hw(name: str) -> bool:
    return "hw:" in name.lower()


def _pick_device(devs: list[tuple[int, str]], default: str | None, spec: str, shared: bool) -> str | None:
    """The device the app ends up using: a named match, else PipeWire/pulse, else the system default."""
    if spec:
        named = [n for _, n in devs if spec.lower() in n.lower()]
        if shared:
            soft = [n for n in named if not _is_raw_hw(n)]
            if soft:
                return soft[0]
            for host in ("pipewire", "pulse", "default"):
                for _, n in devs:
                    if n.lower() == host:
                        return n
        return named[0] if named else None
    if shared:
        for host in ("pipewire", "pulse"):
            for _, n in devs:
                if n.lower() == host:
                    return n
    return default or (devs[0][1] if devs else None)


def _shared_flag(ctx: Ctx) -> bool:
    return ctx.cfg("AUDIO_SHARED", "true").lower() in ("1", "true", "yes", "on")


def _mic_name(ctx: Ctx) -> str | None:
    devs, default = ctx.devices("input")
    return _pick_device(devs, default, ctx.cfg("AUDIO_INPUT_DEVICE"), _shared_flag(ctx))


def probe_mic(ctx: Ctx) -> list[Check]:
    devs, default = ctx.devices("input")
    if not devs:
        return [Check("mic", "missing", "no input device", "plug in the XVF3800 or enable the WM8960 overlay; "
                      "see INSTALL.md", True)]
    spec = ctx.cfg("AUDIO_INPUT_DEVICE")
    shared = _shared_flag(ctx)
    name = _pick_device(devs, default, spec, shared)
    if name is None:
        return [Check("mic", "degraded", f"AUDIO_INPUT_DEVICE={spec!r} not found; default source used",
                      "fix AUDIO_INPUT_DEVICE in .env (a name substring from `python -m sounddevice`)", True)]
    if shared and not _is_raw_hw(name):
        return [Check("mic", "ok", f"{name} (shared via PipeWire)", "", True)]
    if shared:
        return [Check("mic", "degraded", f"{name} (raw hw, not shared)",
                      "start PipeWire so the source is shared: systemctl --user start pipewire pipewire-pulse", True)]
    return [Check("mic", "ok", f"{name} (exclusive)", "", True)]


def probe_echo(ctx: Ctx) -> list[Check]:
    cards = _alsa_cards(ctx)
    name = (_mic_name(ctx) or "").lower()
    if "xvf3800" in name:
        xvf = True
    elif "wm8960" in name:
        xvf = False
    elif "xvf3800" in cards and "wm8960" in cards:
        xvf = "xvf3800" in (ctx.run(["pactl", "get-default-source"], 3.0) or "").lower()
    else:
        xvf = "xvf3800" in cards
    if xvf:
        return [Check("echo path", "ok", "XVF3800: hardware AEC")]
    kind = "WM8960" if "wm8960" in cards else "unknown card"
    try:
        importlib.import_module("cabin.frontend.aec")
    except Exception:                                    # noqa: BLE001
        return [Check("echo path", "degraded", f"{kind}: software AEC (pending)",
                      "software AEC not installed yet; half-duplex gating is used meanwhile")]
    return [Check("echo path", "ok", f"{kind}: software AEC")]


def probe_speaker(ctx: Ctx) -> list[Check]:
    devs, default = ctx.devices("output")
    if not devs:
        return [Check("speaker", "missing", "no output device", "connect a speaker; check `aplay -l`", True)]
    spec = ctx.cfg("AUDIO_OUTPUT_DEVICE")
    if spec and not spec.isdigit() and not any(spec.lower() in n.lower() for _, n in devs):
        return [Check("speaker", "degraded", f"AUDIO_OUTPUT_DEVICE={spec!r} not found; system default used",
                      "fix AUDIO_OUTPUT_DEVICE in .env", True)]
    return [Check("speaker", "ok", spec or default or "system default output", "", True)]


def read_f455_modules(ctx: Ctx) -> dict[str, str]:
    """Module versions from the RealSense ID CLI."""
    repo = ctx.env.get("AIBOX_REALSENSE_REPO") or str(ctx.home / "repos" / "RealSenseID")
    cli = os.path.join(repo, "build-device", "bin", "rsid-cli")
    if not os.access(cli, os.X_OK):
        return {}
    out = _run([cli], 12.0, stdin="v\nq\n") or ""
    return dict(re.findall(r"^([A-Z0-9]+)\s*:\s*([0-9][0-9.]*)", out, re.MULTILINE))


def probe_f455(ctx: Ctx) -> list[Check]:
    cams = ctx.glob("/dev/v4l/by-id/usb-Intel_Intel_F45*-video-index0")
    if not cams:
        return [Check("F455 face camera", "degraded", "not connected; face step falls back to voice and PIN",
                      "plug the F455 into a USB 3 port")]
    try:
        importlib.import_module("driveos.biometrics.f455").load_sdk()
        sdk = "SDK ok"
    except Exception:                                    # noqa: BLE001
        sdk = "SDK (rsid_py) not found"
    mods = read_f455_modules(ctx)
    opfw = mods.get("OPFW")
    if not opfw:
        return [Check("F455 face camera", "degraded", f"present, firmware unreadable; {sdk}",
                      "install the RealSense ID tools (see INSTALL.md)")]
    if opfw != F455_TARGET_OPFW:
        return [Check("F455 face camera", "degraded", f"OPFW {opfw}, needs {F455_TARGET_OPFW}; {sdk}",
                      "update the F455 firmware (see INSTALL.md)")]
    return [Check("F455 face camera", "ok", f"OPFW {opfw}; {sdk}")]


def probe_gps(ctx: Ctx) -> list[Check]:
    source = ctx.cfg("GPS_SOURCE", "auto")
    if source.lower() == "sim":
        return [Check("GPS", "degraded", "GPS_SOURCE=sim (simulated position)", "set GPS_SOURCE=auto in .env")]
    port = ctx.cfg("GPS_SERIAL_DEVICE", "/dev/ttyUSB0")
    ports = ctx.glob("/dev/serial/by-id/usb-1a86_*") + ctx.glob(port) + ctx.glob(GPS_PORT)
    if not ports:
        return [Check("GPS", "degraded", "no receiver found; simulated position with a badge",
                      "plug in the USB GPS (needs sky view); check `ls /dev/serial/by-id`")]
    out = ctx.run(["gpspipe", "-w", "-n", "8"], 4.0)
    if out is None:
        return [Check("GPS", "degraded", f"receiver at {ports[0]}, gpsd not answering",
                      "sudo systemctl enable --now gpsd; check /etc/default/gpsd DEVICES")]
    m = re.search(r'"class":"TPV"[^}]*?"mode":(\d)', out)
    mode = int(m.group(1)) if m else 0
    if mode >= 2:
        return [Check("GPS", "ok", f"{mode}D fix via gpsd")]
    return [Check("GPS", "degraded", "receiver up, no fix yet", "move the antenna to a window or outdoors")]


def probe_webcam(ctx: Ctx) -> list[Check]:
    names = []
    for p in sorted(ctx.glob("/sys/class/video4linux/video*/name")):
        n = (ctx.read(p) or "").strip()
        if n and WEBCAM_RE.search(n) and n not in names:
            names.append(n)
    if not names:
        return [Check("webcam", "degraded", "none found; cabin vision disabled", "plug in the C940e/C920e webcam")]
    return [Check("webcam", "ok", ", ".join(names))]


def probe_hailo(ctx: Ctx) -> list[Check]:
    out = ctx.run(["hailortcli", "fw-control", "identify"], 4.0)
    if out is None:
        return [Check("Hailo / HailoRT", "degraded", "hailortcli not available or no device; cloud brain only",
                      "install HailoRT 5.3 and the HAT driver")]
    m = re.search(r"Firmware Version:\s*(\S+)", out)
    board = re.search(r"Board Name:\s*(.+)", out)
    return [Check("Hailo / HailoRT", "ok",
                  f"firmware {m.group(1) if m else '?'}" + (f", {board.group(1).strip()}" if board else ""))]


def _model_row(name: str, path: Path | None, fix: str, required: bool = True) -> Check:
    if path is not None and path.is_file():
        return Check(f"model {name}", "ok", str(path).replace(str(Path.home()), "~"), "", required)
    return Check(f"model {name}", "missing", "not found", fix, required)


def probe_models(ctx: Ctx) -> list[Check]:
    fix = "./setup.sh (models step)"
    rows: list[Check] = []
    try:
        from cabin.providers.turn import _paths
        silero, smart = _paths()
    except Exception:                                    # noqa: BLE001
        silero = smart = None
    rows.append(_model_row("Silero VAD", silero, fix))
    rows.append(_model_row("Smart Turn", smart, fix))
    try:
        from driveos.config import VoiceConfig
        from driveos.voice.parakeet_stt import resolve_paths
        lib, model = resolve_paths(VoiceConfig())
    except Exception:                                    # noqa: BLE001
        lib = model = None
    if lib and model and Path(lib).is_file() and Path(model).is_file():
        rows.append(Check("model Parakeet", "ok", Path(model).name, "", True))
    else:
        rows.append(Check("model Parakeet", "missing", "library or model not found", fix, True))
    try:
        from cabin.providers.tts_phonon import model_cached
        cached = model_cached()
    except Exception:                                    # noqa: BLE001
        cached = False
    rows.append(Check("model Phonon", "ok" if cached else "missing",
                      "cached" if cached else "ptts or weights missing", "" if cached else fix, True))
    return rows


def probe_map(ctx: Ctx) -> list[Check]:
    d = ROOT / "packaging" / "map-service"
    if d.is_dir() and any(d.iterdir()):
        return [Check("map service", "ok", "prebuilt 3D map service installed")]
    return [Check("map service", "degraded", "not installed; the 2D map is used",
                  "place map-service-*.tar.gz in packaging/vendor and run ./setup.sh")]


def probe_keys(ctx: Ctx) -> list[Check]:
    rows = []
    for name, required in KEYS:
        have = bool(ctx.cfg(name))
        rows.append(Check(f"key {name}", "ok" if have else ("missing" if required else "degraded"),
                          "set" if have else "not set",
                          "" if have else f"add {name}= to .env", required))
    return rows


def probe_memory(ctx: Ctx) -> list[Check]:
    if ctx.cfg("MEMORY", "on").lower() not in ("1", "true", "yes", "on"):
        return [Check("memory", "degraded", "off (in-process history only)", "set MEMORY=on in .env")]
    from cabin.memory import memory_status
    base = ctx.env.get("NOVA_CABIN_DATA") or str(ctx.home / ".local" / "share" / "nova-cabin")
    path = Path(base) / "memory.db"
    try:
        st = memory_status(path)
    except Exception as e:                                # noqa: BLE001
        return [Check("memory", "degraded", f"db unreadable ({type(e).__name__})", f"check {path}")]
    mined = (time.strftime("%Y-%m-%d %H:%M", time.localtime(st["last_mined"]))
             if st["last_mined"] else "never")
    return [Check("memory", "ok", f"on, {path}, {st['sessions']} sessions, last mined {mined}")]


def probe_network(ctx: Ctx) -> list[Check]:
    ok, detail = ctx.head("https://openrouter.ai", 3.0)
    if ok:
        return [Check("network", "ok", f"openrouter.ai {detail}")]
    return [Check("network", "degraded", f"openrouter.ai unreachable ({detail})",
                  "check the Wi-Fi or ethernet link; cloud brain, maps and search need it")]


def probe_disk(ctx: Ctx) -> list[Check]:
    free = shutil.disk_usage(ctx.home).free / 1e9
    state: State = "ok" if free >= 3 else ("degraded" if free >= 1 else "missing")
    return [Check("disk", state, f"{free:.1f} GB free in $HOME",
                  "" if state == "ok" else "free space: clear ~/.cache and old logs")]


def probe_tmp(ctx: Ctx) -> list[Check]:
    free = shutil.disk_usage(tempdir()).free / 1e6
    state: State = "ok" if free >= 500 else "degraded"
    return [Check("/tmp free", state, f"{free:.0f} MB",
                  "" if state == "ok" else "/tmp is a RAM disk; scratch files belong in ~/.cache/nova-cabin")]


def tempdir() -> str:
    return "/tmp" if os.path.isdir("/tmp") else "."


def probe_temp(ctx: Ctx) -> list[Check]:
    t = ctx.run(["vcgencmd", "measure_temp"], 3.0)
    th = ctx.run(["vcgencmd", "get_throttled"], 3.0)
    if t is None:
        return [Check("temperature", "ok", "n/a (no vcgencmd)")]
    m = re.search(r"temp=([\d.]+)", t)
    temp = float(m.group(1)) if m else None
    bits = re.search(r"0x([0-9a-fA-F]+)", th or "")
    flags = int(bits.group(1), 16) if bits else 0
    now = bool(flags & 0xF)
    detail = f"{temp:.1f} C" if temp is not None else "unreadable"
    detail += f", throttled 0x{flags:x}" if bits else ", throttled n/a"
    hot = temp is not None and temp >= 75
    return [Check("temperature", "degraded" if (hot or now) else "ok", detail,
                  "improve airflow; the app lowers its load above 75 C" if (hot or now) else "")]


PROBES: list[tuple[str, Callable[[Ctx], list[Check]]]] = [
    ("mic", probe_mic), ("echo path", probe_echo), ("speaker", probe_speaker), ("F455", probe_f455),
    ("GPS", probe_gps), ("webcam", probe_webcam), ("Hailo", probe_hailo), ("models", probe_models),
    ("map service", probe_map), ("keys", probe_keys), ("memory", probe_memory),
    ("network", probe_network), ("disk", probe_disk),
    ("/tmp", probe_tmp), ("temperature", probe_temp),
]


PROBE_TIMEOUTS = {"F455": 16.0}
OVERALL_CAP_S = 20.0


def _start(name: str, fn: Callable[[Ctx], list[Check]], ctx: Ctx):
    box: list[list[Check]] = []
    err: list[str] = []

    def work() -> None:
        try:
            box.append(fn(ctx))
        except Exception as e:                           # noqa: BLE001
            err.append(type(e).__name__)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t, box, err


def report(ctx: Ctx | None = None, probes=None, timeout: float | None = None,
           cap: float = OVERALL_CAP_S) -> list[Check]:
    """Run every probe concurrently; each has its own budget, all together at most ``cap`` seconds."""
    try:
        ctx = ctx or Ctx()
    except Exception as e:                               # noqa: BLE001
        return [Check("config", "missing", f"cannot read settings ({type(e).__name__})",
                      "fix the syntax of .env", True)]
    started = [(name, *_start(name, fn, ctx), timeout if timeout is not None else PROBE_TIMEOUTS.get(name, PROBE_TIMEOUT_S))
               for name, fn in (probes if probes is not None else PROBES)]
    t0 = time.monotonic()
    out: list[Check] = []
    for name, t, box, err, budget in started:
        t.join(max(0.0, min(budget, cap - (time.monotonic() - t0))))
        if box:
            out.extend(box[0])
        else:
            why = f"probe failed ({err[0]})" if err else f"probe timed out after {budget:g} s"
            out.append(Check(name, "degraded", why, "re-run; if it repeats, check the device"))
    return out


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.required and c.state == "missing" for c in checks) else 0


def render(checks: list[Check]) -> str:
    w1 = max(len(c.name) for c in checks)
    w2 = max(len(c.detail) for c in checks)
    lines = [f"{'STATE':<9} {'CHECK':<{w1}}  {'DETAIL':<{w2}}  FIX", "-" * (w1 + w2 + 30)]
    for c in checks:
        lines.append(f"{c.state.upper():<9} {c.name:<{w1}}  {c.detail:<{w2}}  {c.fix}".rstrip())
    n = {s: sum(c.state == s for c in checks) for s in ("ok", "degraded", "missing")}
    lines.append("")
    lines.append(f"{n['ok']} ok, {n['degraded']} degraded, {n['missing']} missing"
                 + ("" if exit_code(checks) == 0 else "  (a required row is missing)"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Nova Cabin doctor")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    checks = report()
    if args.json:
        print(json.dumps({"ok": exit_code(checks) == 0, "checks": [asdict(c) for c in checks]}, indent=2))
    else:
        print(render(checks))
    return exit_code(checks)


if __name__ == "__main__":
    sys.exit(main())
