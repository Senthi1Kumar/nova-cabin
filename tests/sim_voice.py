"""Simulation mode with a (synthetic) microphone: VAD → SimSTT partials → offline brain → speech → barge-in."""
import asyncio, sys, tempfile, time
from pathlib import Path
import numpy as np
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cabin.audio import FRAME
from cabin.config import Settings
from cabin.engine import Engine

T0 = time.monotonic()
log = []
def emit(k, v):
    if k in ("state", "card", "toast") or (k == "user" and v["text"]):
        if k == "card" and v: v = f"[{v['kind']}]"
        if k == "user": v = ("…" if v["partial"] else "✓ ") + v["text"]
        log.append((k, v)); print(f"{time.monotonic()-T0:5.1f}s {k:<6} {v}", flush=True)

rng = np.random.default_rng(0)
def fr(kind):
    if kind == "noise": return (rng.standard_normal(FRAME) * 10 ** (-62 / 20)).astype(np.float32)
    t = np.arange(FRAME) / 16000
    return (0.1 * np.sin(2 * np.pi * 170 * t) + rng.standard_normal(FRAME) * 0.02).astype(np.float32)

async def feed(e, kind, secs):
    for _ in range(int(secs * 50)):
        e._on_frame(fr(kind)); await asyncio.sleep(0.02)

async def main():
    cfg = Settings.load(ROOT / "tests/empty.env"); cfg.raw["SIM_VOICE"] = "murmur"
    e = Engine(cfg, emit, Path(tempfile.mkdtemp()), sim=True, autopilot=False)
    await e.start(); e.voice_ok = True
    await feed(e, "noise", 1.2)
    await feed(e, "speech", 2.6)        # driver talks (transcript = scripted request 1)
    await feed(e, "noise", 5.0)
    await feed(e, "speech", 0.8)        # barge-in over the reply
    await feed(e, "noise", 6.0)
    states = [v for k, v in log if k == "state"]
    print("STATES", states)
    assert "speaking" in states and states.count("listening") >= 2, "expected two listening phases (turn + barge-in)"
    print("PASS"); await e.stop()
asyncio.run(main())
