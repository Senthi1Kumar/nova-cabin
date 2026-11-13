"""Tool-call guarantee (nudge, rules path, can't-do rewrite) and freshness stamps."""
import asyncio
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

from cabin.brain import NUDGE, Planner, label_stale, needs_tool, stamp
from cabin.brain.freshness import PROMPT_LINE
from cabin.config import Settings
from cabin.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = [{"type": "function", "function": {"name": n}} for n in
           ("shop_find_places", "find_places", "get_weather", "navigate_to", "shop_view_cart")]


class TextOnly:
    label, model = "fake", "fake"

    def __init__(self, text="Sure, there is one nearby."):
        self.systems, self.text, self.rounds = [], text, 0

    async def stream(self, messages, tools):
        self.systems.append(messages[0]["content"])
        self.rounds += 1
        if any(m.get("role") == "tool" for m in messages):
            yield "text", "Found it."
        else:
            yield "text", self.text


def _engine(llm, schemas=SCHEMAS):
    cfg = Settings.load(ROOT / "tests" / "empty.env")
    eng = Engine(cfg, lambda k, v: None, Path.home() / ".cache" / "nova-cabin" / "test", sim=True)
    eng.messages, eng.llm, eng.ran = [], llm, []

    async def run(name, args):
        eng.ran.append((name, args))
        return {"results": [{"name": "CVS"}]}, None, None
    eng.tools = SimpleNamespace(schemas=schemas, captions={}, run=run)
    eng.planner = Planner(eng)
    eng.spoken = []

    async def speak(q):
        while (s := await q.get()) is not None:
            eng.spoken.append(s)
    eng._speak_loop = speak
    return eng


def test_no_tool_turn_is_retried_once_then_rules():
    eng = _engine(TextOnly())
    asyncio.run(eng._respond("find a pharmacy"))
    assert NUDGE not in eng.llm.systems[0] and NUDGE in eng.llm.systems[1]
    assert eng.ran == [("shop_find_places", {"category": "pharmacy"})]
    assert "there is one nearby" not in " ".join(eng.spoken)


def test_rules_fall_back_to_find_places_without_shop_tool():
    eng = _engine(TextOnly(), [s for s in SCHEMAS if s["function"]["name"] != "shop_find_places"])
    asyncio.run(eng._respond("find a pharmacy"))
    assert eng.ran == [("find_places", {"query": "pharmacy"})]


def test_cant_do_requires_tool_error():
    eng = _engine(TextOnly("I can't do that. Sorry."))
    asyncio.run(eng._respond("find a pharmacy"))
    said = " ".join(eng.spoken)
    assert "can't" not in said and "Let me try that." in said
    assert eng.ran == [("shop_find_places", {"category": "pharmacy"})]


def test_cant_is_allowed_after_a_tool_error():
    class Fails(TextOnly):
        async def stream(self, messages, tools):
            if not any(m.get("role") == "tool" for m in messages):
                yield "tool_calls", [{"id": "a", "name": "find_places", "arguments": json.dumps({"query": "x"})}]
            else:
                yield "text", "I can't reach the places service right now."
    eng = _engine(Fails())

    async def run(name, args):
        return {"error": "places down"}, None, None
    eng.tools.run = run
    asyncio.run(eng._respond("find a pharmacy"))
    assert "can't reach" in " ".join(eng.spoken)


def test_unmapped_request_asks_one_question():
    eng = _engine(TextOnly("I cannot help with that."))
    asyncio.run(eng._respond("is there any news about the thing"))
    assert eng.ran == [] and "Which place do you mean?" in " ".join(eng.spoken)
    assert "cannot" not in " ".join(eng.spoken)


def test_chat_turn_is_untouched():
    eng = _engine(TextOnly("Glad to hear it."))
    asyncio.run(eng._respond("thanks"))
    assert eng.llm.rounds == 1 and eng.ran == [] and "Glad to hear it." in " ".join(eng.spoken)


def test_needs_tool_rules_and_judge_seam():
    assert asyncio.run(needs_tool("find a pharmacy"))
    assert asyncio.run(needs_tool("what's the weather"))
    assert asyncio.run(needs_tool("take me to the airport"))
    assert not asyncio.run(needs_tool("thanks"))

    async def yes(_):
        return True

    async def defer(_):
        return None
    assert asyncio.run(needs_tool("thanks", judge=yes))
    assert not asyncio.run(needs_tool("thanks", judge=defer))


def test_stale_results_labelled():
    now = dt.datetime(2026, 11, 12, 12, 0, tzinfo=dt.UTC)
    fresh = stamp({"a": 1}, now)
    assert fresh["as_of"].startswith("2026-11-12T12:00:00") and "stale" not in label_stale(fresh, now)
    old = {"as_of": (now - dt.timedelta(minutes=11)).isoformat()}
    assert label_stale(old, now)["stale"] is True
    assert "stale" not in label_stale({"as_of": (now - dt.timedelta(minutes=9)).isoformat()}, now)
    assert stamp(old, now)["as_of"] == old["as_of"]


def test_engine_stamps_results_and_prompt_line():
    async def go():
        eng = _engine(TextOnly())
        eng.system_extra.append(PROMPT_LINE)
        await eng._respond("find a pharmacy")
        return eng
    eng = asyncio.run(go())
    tool_msgs = [json.loads(m["content"]) for m in eng.messages if m["role"] == "tool"]
    assert tool_msgs and "as_of" in tool_msgs[0]
    assert "as_of" in eng.llm.systems[0]


import pytest


@pytest.mark.parametrize("text", ["my head hurts", "I'm a big fan of jazz", "tell me a joke", "thank you",
                                  "how are you", "what can you do", "what's the date today"])
def test_chat_phrases_do_not_need_a_tool(text):
    assert not asyncio.run(needs_tool(text))


def test_zero_result_then_cant_is_spoken_without_second_tool_call():
    class ZeroThenCant(TextOnly):
        async def stream(self, messages, tools):
            if not any(m.get("role") == "tool" for m in messages):
                yield "tool_calls", [{"id": "a", "name": "shop_find_places",
                                      "arguments": json.dumps({"category": "pharmacy"})}]
            else:
                yield "text", "I can't find any pharmacies nearby."
    eng = _engine(ZeroThenCant())

    async def run(name, args):
        eng.ran.append((name, args))
        return {"results": []}, None, None
    eng.tools.run = run
    asyncio.run(eng._respond("find a pharmacy"))
    assert len(eng.ran) == 1 and "can't find any pharmacies" in " ".join(eng.spoken)


def test_cant_with_tool_calls_does_not_silence_next_round():
    class CantAndCall(TextOnly):
        async def stream(self, messages, tools):
            if not any(m.get("role") == "tool" for m in messages):
                yield "text", "I can't do that. "
                yield "tool_calls", [{"id": "a", "name": "find_places", "arguments": json.dumps({"query": "x"})}]
            else:
                yield "text", "Here is what I found nearby."
    eng = _engine(CantAndCall())
    asyncio.run(eng._respond("find a coffee shop"))
    assert "Here is what I found nearby." in " ".join(eng.spoken)
