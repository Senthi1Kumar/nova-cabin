"""Hands-free voice loop with synthetic mic audio (noise floor, speech, speaker echo, barge-in)."""
import asyncio, os, subprocess, sys, time
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
srv = subprocess.Popen([sys.executable, str(ROOT / "tests/mock_openai.py"), "8767"])
time.sleep(0.6)
os.environ.update(NO_PROXY="127.0.0.1", no_proxy="127.0.0.1", OPENAI_API_KEY="t",
                  OPENAI_BASE_URL="http://127.0.0.1:8767/v1", LLM_PROVIDER="openai", OPENAI_MODEL="m",
                  TTS_PROVIDER="openai", STT_PROVIDER="openai")
import offline_services; offline_services.install()
from cabin.audio import FRAME
from cabin.config import Settings
from cabin.engine import Engine

T0 = time.monotonic()
seen = []
def emit(k, v):
    if k in ("state", "user", "toast") :
        seen.append((k, v)); print(f"{time.monotonic()-T0:6.2f}s  {k:<6} {v}", flush=True)

rng = np.random.default_rng(1)
def frame(kind, eng):
    if kind == "noise":
        return (rng.standard_normal(FRAME) * 10 ** (-62 / 20)).astype(np.float32)
    if kind == "speech":
        t = np.arange(FRAME) / 16000
        return (0.1 * np.sin(2 * np.pi * 180 * t) + rng.standard_normal(FRAME) * 0.02).astype(np.float32)
    if kind == "echo":   # speaker bleed ~22 dB below playback
        lvl = 10 ** ((eng.player.level_db - 22) / 20) if eng.player.playing else 10 ** (-62 / 20)
        return (rng.standard_normal(FRAME) * lvl).astype(np.float32)

async def feed(eng, kind, secs):
    for _ in range(int(secs * 50)):
        eng._on_frame(frame(kind, eng)); await asyncio.sleep(0.02)

async def main():
    eng = Engine(Settings.load(ROOT / "tests/empty.env"), emit, Path("/tmp/cabin-cache"))
    await eng.start()
    eng.voice_ok = True                         # no PortAudio here; inject frames directly
    print("— silence 1.5 s (learn noise floor)"); await feed(eng, "noise", 1.5)
    print("— driver speaks 1.4 s");               await feed(eng, "speech", 1.4)
    print("— silence; assistant replies with echo on the mic"); await feed(eng, "echo", 4.0)
    falsebarge = [x for x in seen if x == ("state", "listening")]
    print("— driver talks over the assistant");  await feed(eng, "speech", 0.6)
    await feed(eng, "noise", 0.3)
    print("states:", [v for k, v in seen if k == "state"])
    print("listening entries before barge:", len(falsebarge))
    await eng.stop()
try: asyncio.run(main())
finally: srv.terminate()
