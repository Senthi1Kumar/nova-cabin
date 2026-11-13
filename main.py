"""Cabin Assistant — entry point.

    python main.py                    # live (keys in .env); falls back to simulation without an LLM key
    python main.py --sim              # simulation: every feature offline, no API keys, you drive it
    python main.py --demo             # simulation + autopilot guided tour of all features
    python main.py --fullscreen       # kiosk / head-unit
    python main.py --env path/.env    # alternate config
    python main.py --fps              # performance HUD (toggle with D)
    python main.py --state speaking --shot out.png          # render a frozen frame and exit

Keys: Space talk/stop · K keyboard · D HUD · F fullscreen
      simulation: N next tour step · P pause/resume tour
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Slot
from PySide6.QtGui import QFontDatabase, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

from cabin import telemetry
from cabin.avatar import load_rig
from cabin.config import Settings
from cabin.controller import AssistantController
from cabin.icons import IconProvider
from cabin.lipsync import LipSyncDriver
from cabin.nav.live_map import NovaMapProvider
from cabin.services.face_preview import FacePreviewProvider
from cabin.sim import TOUR, demo_card
from cabin.telemetry.qt import make as make_diagnostics

ROOT = Path(__file__).resolve().parent


class SimControls(QObject):
    """Keyboard shortcuts for the simulation tour (QML context property `simulator`)."""

    def __init__(self, runtime) -> None:
        super().__init__()
        self.rt = runtime

    @Slot()
    def next(self) -> None:
        self.rt.call(self.rt.engine.sim_next)

    @Slot()
    def togglePause(self) -> None:
        self.rt.call(self.rt.engine.sim_toggle)


def _let_signals_run() -> None:
    """A no-op tick so Python can run a signal handler while Qt is inside exec()."""


def _install_quit(app: QGuiApplication) -> None:
    """Ctrl+C and SIGTERM quit the window. The default handler raises inside whatever
    Python the QML frame is running, which is the ``level`` property read."""

    def request(*_args: object) -> None:
        QTimer.singleShot(0, app.quit)

    signal.signal(signal.SIGINT, request)
    signal.signal(signal.SIGTERM, request)
    pulse = QTimer(app)
    pulse.setInterval(200)
    pulse.timeout.connect(_let_signals_run)
    pulse.start()


def cache_dir() -> Path:
    base = Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache")
    d = base / "CabinAssistant"
    d.mkdir(parents=True, exist_ok=True)
    return d


DISPLAY_KEYS = ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM")


def use_local_display(cfg) -> None:
    """Draw on the device's own screen even when started from an SSH login.

    Values set in .env win; otherwise a Linux session with no display set uses the
    running Wayland desktop of this user, when there is one."""
    for key in DISPLAY_KEYS:
        if cfg.get(key) and not os.environ.get(key):
            os.environ[key] = cfg.get(key)
    if sys.platform != "linux" or os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"):
        return
    run = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    sock = next(iter(sorted(run.glob("wayland-[0-9]"))), None) if run.is_dir() else None
    if sock is None:
        return
    os.environ.setdefault("XDG_RUNTIME_DIR", str(run))
    os.environ["WAYLAND_DISPLAY"] = sock.name
    os.environ.setdefault("QT_QPA_PLATFORM", "wayland")
    logging.getLogger("main").info("display: %s (local desktop)", sock.name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", action="store_true", help="simulation mode: offline providers, no API keys")
    ap.add_argument("--demo", action="store_true", help="simulation + autopilot tour")
    ap.add_argument("--autopilot", dest="autopilot", action="store_true", default=None)
    ap.add_argument("--no-autopilot", dest="autopilot", action="store_false")
    ap.add_argument("--env", help="path to .env (default: ./.env)")
    ap.add_argument("--fullscreen", action="store_true")
    ap.add_argument("--fps", action="store_true")
    ap.add_argument("--diagnostics", action="store_true", help="open the Diagnostics panel at start (toggle with D)")
    ap.add_argument("--fps-log", action="store_true", help="with --fps: print the HUD fps every 0.5 s (stderr)")
    ap.add_argument("--state", choices=["idle", "listening", "thinking", "speaking"],
                    help="demo: freeze on one state")
    ap.add_argument("--shot")
    ap.add_argument("--delay", type=int, default=1800)
    ap.add_argument("--frames", type=int, default=0)
    ap.add_argument("--say", help="submit this request after startup (testing)")
    args, qt_args = ap.parse_known_args()

    cfg = Settings.load(Path(args.env) if args.env else None)
    logging.basicConfig(level=getattr(logging, cfg.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    telemetry.install_logging(cfg)

    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
    use_local_display(cfg)
    app = QGuiApplication([sys.argv[0], *qt_args])
    _install_quit(app)
    app.setApplicationName("Cabin Assistant")
    app.setOrganizationName("Elevatics")
    for f in (ROOT / "assets" / "fonts").glob("*.[ot]tf"):
        QFontDatabase.addApplicationFont(str(f))

    ctrl = AssistantController(app)
    no_llm = cfg.llm() is None
    simulate = args.sim or args.demo or cfg.flag("SIMULATION") or no_llm
    autopilot = args.autopilot if args.autopilot is not None else \
        (args.demo or (no_llm and not args.sim and not cfg.flag("SIMULATION")) or cfg.flag("SIM_AUTOPILOT"))
    sim = runtime = None

    if args.state:                                          # frozen frame for screenshots
        ctrl.state, ctrl.level = args.state, 0.5
        if args.state != "idle":
            ctrl.apply("user", {"text": TOUR[0].say, "partial": args.state == "listening"})
        if args.state == "speaking":
            ctrl.replyText = "Your trip to Lake Tahoe is 198 miles, about 3 hours 42 minutes of driving."
            ctrl.card = demo_card()
        ctrl.caption = {"listening": "Listening", "thinking": "Thinking"}.get(args.state, "")
        ctrl.apply("outside", "64°F")
    else:
        from cabin.runtime import Runtime
        runtime = Runtime(cfg, cache_dir(), sim=simulate, autopilot=autopilot)
        if simulate:
            sim = SimControls(runtime)
        ctrl.lips = LipSyncDriver(ctrl, ctrl)
        runtime.bridge.update.connect(ctrl.apply)
        eng = runtime.engine
        ctrl.micPressed.connect(lambda: runtime.call(eng.tap))
        ctrl.textSubmitted.connect(lambda t: runtime.call(eng.submit_text, t))
        ctrl.actionTriggered.connect(lambda k, p: runtime.call(eng.action, k, dict(p)))
        ctrl.cabinTempNudged.connect(lambda d: runtime.call(eng.set_cabin_temp, d))
        ctrl.seatHeatCycled.connect(lambda: runtime.call(eng.set_seat_heat))
        ctrl.locationMapRequested.connect(
            lambda on: runtime.call(eng.show_location_map if on else eng.hide_location_map))
        if no_llm and not (args.sim or args.demo or cfg.flag("SIMULATION")):
            QTimer.singleShot(4200, lambda: ctrl.toast.emit(
                "No LLM key in .env — running the simulation tour"))

    engine = QQmlApplicationEngine()
    engine.addImageProvider("icons", IconProvider())
    engine.addImageProvider("novamap", NovaMapProvider())
    engine.addImageProvider("facepreview", FacePreviewProvider())
    ctx = engine.rootContext()
    ctx.setContextProperty("assistant", ctrl)
    ctx.setContextProperty("showPerf", args.fps or args.diagnostics)
    ctx.setContextProperty("diagnostics", make_diagnostics(runtime))
    ctx.setContextProperty("perfLog", bool(args.fps and args.fps_log))
    ctx.setContextProperty("driverName", cfg.get("DRIVER_NAME", "Driver"))
    ctx.setContextProperty("avatarRig", load_rig(ROOT / "assets" / "avatar"))
    if sim:
        ctx.setContextProperty("simulator", sim)
    engine.load(QUrl.fromLocalFile(str(ROOT / "qml" / "Main.qml")))
    if not engine.rootObjects():
        return 1
    win = engine.rootObjects()[0]
    if args.fullscreen:
        win.showFullScreen()

    if runtime:
        runtime.start()
        if args.say:
            QTimer.singleShot(800, lambda: ctrl.submitText(args.say))

    if args.shot and args.frames:
        out = Path(args.shot)
        out.mkdir(parents=True, exist_ok=True)
        n = {"i": 0}

        def tick() -> None:
            win.grabWindow().save(str(out / f"f{n['i']:03d}.png"))
            n["i"] += 1
            if n["i"] >= args.frames:
                app.quit()
            else:
                QTimer.singleShot(66, tick)
        QTimer.singleShot(args.delay, tick)
    elif args.shot:
        def grab() -> None:
            win.grabWindow().save(args.shot)
            app.quit()
        QTimer.singleShot(args.delay, grab)

    rc = app.exec()
    if runtime:
        runtime.shutdown()
    del engine
    return rc


if __name__ == "__main__":
    sys.exit(main())
