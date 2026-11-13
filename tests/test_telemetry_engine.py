"""Engine hooks: every emit, per-turn spans, tool I/O reach telemetry; telemetry can never break a turn."""
import asyncio
import tempfile
import time
from pathlib import Path

from cabin.config import Settings
from cabin.engine import Engine
from cabin.telemetry.spans import TurnSpans

ROOT = Path(__file__).resolve().parents[1]


class Recorder:
    def __init__(self):
        self.events, self.tools, self.llms, self.records, self.spans = [], [], [], [], []

    def event(self, kind, value=None):
        self.events.append(kind)

    def turn(self):
        ts = TurnSpans(f"t{len(self.spans)}", on_done=self.records.append)
        self.spans.append(ts)
        return ts

    def tool(self, name, args, result, duration_s, agent=None, error=None):
        self.tools.append((name, args, result, duration_s, error))

    def llm(self, *a):
        self.llms.append(a)


class Exploding:
    def __getattr__(self, name):
        def boom(*a, **k):
            raise RuntimeError("telemetry down")
        return boom


async def run_turn(tel, text="Find a coffee shop nearby"):
    out = []
    cfg = Settings.load(ROOT / "tests" / "empty.env")
    cfg.raw["SIM_VOICE"] = "murmur"
    eng = Engine(cfg, lambda k, v: out.append(k), Path(tempfile.mkdtemp()), sim=True)
    await eng.start()
    eng.telemetry = tel
    eng.submit_text(text)
    t0 = time.monotonic()
    await asyncio.sleep(0.3)
    while (eng.state != "idle" or time.monotonic() - t0 < 1) and time.monotonic() - t0 < 60:
        await asyncio.sleep(0.1)
    await eng.stop()
    return out


def test_turn_reaches_telemetry():
    rec = Recorder()
    asyncio.run(run_turn(rec))
    assert "user" in rec.events and "state" in rec.events and "card" in rec.events
    assert rec.tools and rec.tools[0][0].startswith("shop_") and rec.tools[0][3] >= 0
    assert len(rec.records) == 1
    r = rec.records[0]
    for f in ("first_token_ms", "tools_ms", "first_audio_ms", "total_ms"):
        assert r[f] is not None, f
    m = rec.spans[0]._marks
    order = [n for n in ("speech_end", "first_token", "tools_done", "tts_first_chunk", "done") if n in m]
    assert order == ["speech_end", "first_token", "tools_done", "tts_first_chunk", "done"]
    assert m["speech_end"] <= m["first_token"] <= m["tools_done"] <= m["tts_first_chunk"] <= m["done"]
    assert "first_audio" in m or r["first_audio_ms"] is not None
    assert rec.llms


def test_broken_telemetry_does_not_break_turn():
    ok = asyncio.run(run_turn(Recorder()))
    bad = asyncio.run(run_turn(Exploding()))
    assert "card" in bad and bad.count("toast") <= ok.count("toast")
    assert [k for k in ok if k in ("card", "state")] == [k for k in bad if k in ("card", "state")]


def test_missing_telemetry_is_fine():
    out = asyncio.run(run_turn(None))
    assert "card" in out
