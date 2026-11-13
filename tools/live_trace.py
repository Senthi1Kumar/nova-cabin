"""Headless live run: the real OpenRouter model, on-device STT/TTS providers and the commerce/payment plugins.

    python tools/live_trace.py "Find a coffee shop nearby" "Open the first one" "Add a latte" "I'm ready to pay"

Needs the keys in .env. Prints the event stream and the time from request to first spoken sentence.
"""
import asyncio
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("NOVA_CABIN_DATA", tempfile.mkdtemp())
from cabin.config import Settings
from cabin.engine import Engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)-7s %(message)s", datefmt="%H:%M:%S")
for noisy in ("httpx", "httpcore", "websockets"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
T0 = time.monotonic()
QUIET = {"level", "words", "lips_stop", "vehicle", "providers", "reply"}
first_speech: list[float] = []


def emit(k, v):
    if k == "state" and v == "speaking" and not first_speech:
        first_speech.append(time.monotonic())
    if k in QUIET:
        return
    if k == "card" and v:
        v = f"[{v['kind']}] {v.get('title', '')} | " + " | ".join(str(r.get("value", "")) for r in v.get("rows", [])[:4])
    if k == "checkout" and v:
        v = {x: v.get(x) for x in ("stage", "method", "total", "otp_pending")}
    print(f"{time.monotonic() - T0:6.1f}s  {k:<9} {v}", flush=True)


async def main(requests):
    cfg = Settings.load(Path(__file__).resolve().parents[1] / ".env")
    eng = Engine(cfg, emit, Path(tempfile.mkdtemp()), sim=False, autopilot=False)
    await eng.start()
    print("llm:", eng.llm.label if eng.llm else None, eng.llm.model if eng.llm else "", "| stt:", getattr(eng.stt, "label", None),
          "| tts:", getattr(eng.tts, "label", None))
    for r in requests:
        first_speech.clear()
        t = time.monotonic()
        print(f"-- driver: {r}")
        if not await eng._intercept(r):
            eng._turn = asyncio.current_task()
            await eng._respond(r)
        while eng.state != "idle":
            await asyncio.sleep(0.1)
        ttfs = f"{first_speech[0] - t:.2f}s" if first_speech else "n/a"
        print(f"{'':8}  said      {eng.messages[-1]['content'] if eng.messages else ''}  (first speech {ttfs})")
    await eng.stop()


asyncio.run(main(sys.argv[1:] or ["Find a coffee shop nearby"]))
