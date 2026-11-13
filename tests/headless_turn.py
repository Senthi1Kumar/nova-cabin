"""Headless end-to-end turn: engine + mock LLM/TTS + fixtures, printing every UI event.
    python tests/headless_turn.py "Plan a weekend trip to Lake Tahoe" ["barge"]"""
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
srv = subprocess.Popen([sys.executable, str(ROOT / "tests/mock_openai.py"), "8766"])
time.sleep(0.6)
os.environ.update(NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
                  OPENAI_API_KEY="test", OPENAI_BASE_URL="http://127.0.0.1:8766/v1", LLM_PROVIDER="openai",
                  OPENAI_MODEL="mock-1", TTS_PROVIDER="openai")
import offline_services
offline_services.install()
from cabin.config import Settings
from cabin.engine import Engine

T0 = time.monotonic()


def emit(k, v):
    if k in ("level",):
        return
    if k == "words":
        v = f"{len(v)} words, first '{v[0][0]}' at +{v[0][1] - time.monotonic():.2f}s" if v else v
    if k == "card" and v:
        v = {"kind": v["kind"], "hero": v.get("hero", {}).get("kind"), "rows": [r["value"] for r in v["rows"]]}
    if k == "nav" and v:
        v = {x: v[x] for x in ("name", "distance", "duration", "eta", "arrival")}
    print(f"{time.monotonic() - T0:6.2f}s  {k:<10} {v}", flush=True)


async def main():
    cfg = Settings.load(ROOT / "tests" / "empty.env")
    eng = Engine(cfg, emit, Path("/tmp/cabin-cache"))
    await eng.start()
    eng.submit_text(sys.argv[1])
    if len(sys.argv) > 2 and sys.argv[2] == "barge":
        await asyncio.sleep(3.0)
        print("------ user taps orb (interrupt) ------")
        eng.tap()
        await asyncio.sleep(0.5)
        eng.submit_text("Find a coffee shop nearby")
    for _ in range(200):
        await asyncio.sleep(0.1)
        if eng.state == "idle" and time.monotonic() - T0 > 2:
            break
    print("history:", [(m["role"], (m.get("content") or "")[:50]) for m in eng.messages])
    await eng.stop()

try:
    asyncio.run(main())
finally:
    srv.terminate()
