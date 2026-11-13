"""Headless simulation: runs the real engine with offline providers and prints the event stream.

    python tools/sim_trace.py                     # autopilot tour (all features), 90 s
    python tools/sim_trace.py "Find tacos near Oakland" "Take me there"   # your own requests
No Qt window, no keys, no network.
"""
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cabin.config import Settings  # noqa: E402
from cabin.engine import Engine  # noqa: E402

T0 = time.monotonic()
QUIET = {"level", "words", "lips_stop", "reply"}


def emit(k, v):
    if k in QUIET:
        return
    if k == "card" and v:
        v = f"[{v['kind']}] " + " | ".join(r["value"] for r in v.get("rows", []))
    if k == "nav" and v:
        v = f"→ {v['name']} {v['distance']} {v['duration']} ETA {v['eta']}"
    if k == "user" and v.get("partial"):
        return
    if k == "user":
        v = v["text"]
    print(f"{time.monotonic() - T0:6.1f}s  {k:<9} {v}", flush=True)


async def main(requests):
    cfg = Settings.load(Path(__file__).resolve().parents[1] / ".env")
    cfg.raw["SIM_VOICE"] = cfg.raw.get("SIM_VOICE") or "murmur"
    eng = Engine(cfg, emit, Path(tempfile.mkdtemp()), sim=True, autopilot=not requests)
    await eng.start()
    if requests:
        for r in requests:
            await eng.simulate_voice(r)
            while eng.state != "idle":
                await asyncio.sleep(0.1)
            print(f"{'':8}  said      {eng.messages[-1]['content']}")
    else:
        await asyncio.sleep(float(90))
    await eng.stop()

asyncio.run(main(sys.argv[1:]))
