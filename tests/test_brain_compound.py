"""Compound requests: parallel tool rounds, templated merge, partial-failure reporting."""
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

from cabin.brain import Planner
from cabin.brain.compound import ToolOutcome, merge_reply, outcome_from
from cabin.config import Settings
from cabin.engine import Engine
from cabin.tools import gather_tools

ROOT = Path(__file__).resolve().parents[1]


def test_compound_runs_tools_in_parallel():
    async def slow(**_):
        await asyncio.sleep(0.3)
        return {"ok": True}, None, None

    box = SimpleNamespace(run=lambda name, args: slow())
    calls = [{"id": "a", "name": "x", "arguments": "{}"}, {"id": "b", "name": "y", "arguments": "{}"}]
    t = time.monotonic()
    out = asyncio.run(gather_tools(box, calls))
    assert len(out) == 2 and time.monotonic() - t < 0.4


def test_merge_reply_templates():
    ok = [ToolOutcome("set_climate", True, "set to 20 degrees", None),
          ToolOutcome("shop_find_places", True, "the nearest shopping centre is Westfield, 8 minutes away", None)]
    assert merge_reply(ok) == "Set to 20 degrees. The nearest shopping centre is Westfield, 8 minutes away."
    assert merge_reply([]) is None


def test_outcome_summaries():
    o = outcome_from("set_climate", {}, {"ok": True, "cabin_temp_f": 68}, metric=True)
    assert o.summary == "set to 20 degrees"
    o = outcome_from("find_places", {"query": "shopping centre"},
                     {"results": [{"name": "Mall", "distance": "2.0 km"}]})
    assert o.summary == "the nearest shopping centre is Mall, 2.0 km away"
    assert not outcome_from("find_places", {"query": "x"}, {"results": []}).ok
    assert outcome_from("web_search", {}, {"error": "boom"}).error == "boom"


class TwoCallsThenSilence:
    label, model = "fake", "fake"

    def __init__(self):
        self.round = 0
        self.systems = []

    async def stream(self, messages, tools):
        self.systems.append(messages[0]["content"])
        self.round += 1
        if self.round == 1:
            yield "tool_calls", [
                {"id": "c1", "name": "set_climate", "arguments": json.dumps({"temperature_f": 68})},
                {"id": "c2", "name": "shop_find_places",
                 "arguments": json.dumps({"query": "shopping centres"})}]


def test_compound_partial_failure_reports_which_half():
    async def go():
        cfg = Settings.load(ROOT / "tests" / "empty.env")
        cfg.raw["UNITS"] = "metric"
        events = []
        eng = Engine(cfg, lambda k, v: events.append((k, v)), Path.home() / ".cache" / "nova-cabin" / "test", sim=True)
        eng.messages = []
        eng.llm = TwoCallsThenSilence()

        async def run(name, args):
            if name == "shop_find_places":
                return {"error": "RuntimeError: places down"}, None, None
            return {"ok": True, "cabin_temp_f": 68}, None, None
        eng.tools = SimpleNamespace(schemas=[], captions={}, run=run)
        eng.planner = Planner(eng)
        spoken = []

        async def speak(q):
            while (s := await q.get()) is not None:
                spoken.append(s)
        eng._speak_loop = speak
        await eng._respond("set it to 20 degrees and find shopping centres")
        return eng, spoken

    eng, spoken = asyncio.run(go())
    reply = " ".join(spoken).lower()
    assert "set to 20 degrees" in reply and "couldn't find shopping centres" in reply
    assert "several parts" in eng.llm.systems[0]
    assert any("places down" in str(m.get("content")) for m in eng.messages if m["role"] == "tool")


def _engine(llm, run, budget=None):
    cfg = Settings.load(ROOT / "tests" / "empty.env")
    cfg.raw["UNITS"] = "metric"
    if budget is not None:
        cfg.raw["TURN_BUDGET_S"] = str(budget)
    eng = Engine(cfg, lambda k, v: None, Path.home() / ".cache" / "nova-cabin" / "test", sim=True)
    eng.messages = []
    eng.llm = llm
    eng.tools = SimpleNamespace(schemas=[], captions={}, run=run)
    eng.planner = Planner(eng)
    spoken = []

    async def speak(q):
        while (s := await q.get()) is not None:
            spoken.append(s)
    eng._speak_loop = speak
    return eng, spoken


async def _ok_run(name, args):
    return {"ok": True, "cabin_temp_f": 68}, None, None


class HangAfterTools(TwoCallsThenSilence):
    async def stream(self, messages, tools):
        if self.round == 0:
            async for ev in super().stream(messages, tools):
                yield ev
        else:
            self.round += 1
            yield "text", "Okay, "
            await asyncio.sleep(30)


def test_compound_deadline_speaks_merge_once():
    async def go():
        eng, spoken = _engine(HangAfterTools(), _ok_run, budget=0.5)
        t = time.monotonic()
        await eng._respond("set it to 20 degrees and find shopping centres")
        return time.monotonic() - t, spoken
    took, spoken = asyncio.run(go())
    assert took < 1.0
    assert len(spoken) == 1 and "set to 20 degrees" in spoken[0].lower()


class SpeaksAfterTools(TwoCallsThenSilence):
    async def stream(self, messages, tools):
        if self.round == 0:
            async for ev in super().stream(messages, tools):
                yield ev
        else:
            yield "text", "Done, it is 20 degrees and the mall is close."


def test_model_text_after_tools_suppresses_merge():
    async def go():
        eng, spoken = _engine(SpeaksAfterTools(), _ok_run)
        await eng._respond("set it to 20 degrees and find shopping centres")
        return spoken
    spoken = asyncio.run(go())
    assert spoken == ["Done, it is 20 degrees and the mall is close."]


def test_rounds_exhausted_with_tools_only_speaks_merge():
    class Loop:
        label = model = "fake"
        n = 0

        async def stream(self, messages, tools):
            self.n += 1
            yield "tool_calls", [{"id": f"c{self.n}", "name": "set_climate",
                                  "arguments": json.dumps({"temperature_f": 60 + self.n})}]

    async def go():
        eng, spoken = _engine(Loop(), _ok_run)
        await eng._respond("set it to 20 degrees and find shopping centres")
        return spoken
    spoken = asyncio.run(go())
    assert len(spoken) == 1 and "set to" in spoken[0].lower()
