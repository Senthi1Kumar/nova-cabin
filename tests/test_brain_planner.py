"""Rules-first planner and the per-turn action ledger."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from cabin.brain import ActionLedger, Planner
from cabin.config import Settings
from cabin.engine import Engine

ROOT = Path(__file__).resolve().parents[1]


def _schema(name, *props):
    return {"type": "function", "function": {"name": name, "parameters": {"properties": {p: {} for p in props}}}}


SHOP_SCHEMAS = [_schema("shop_find_places", "category", "query", "near"), _schema("find_places", "query", "near")]


def fake_engine(metric=True, temp_f=72, schemas=SHOP_SCHEMAS):
    return SimpleNamespace(cfg=SimpleNamespace(metric=metric), vehicle=SimpleNamespace(cabin_temp_f=temp_f),
                           tools=SimpleNamespace(schemas=schemas))


def plan(text, **kw):
    return asyncio.run(Planner(fake_engine(**kw)).before(text))


def test_rules_execute_climate_inside_compound():
    p = plan("set it to 20 degrees and find a shopping centre within 10 minutes")
    assert [(a.name, a.args) for a in p.rule_actions] == [
        ("set_climate", {"temperature_f": 68}),
        ("shop_find_places", {"category": "store", "query": "shopping centre within 10 minutes"})]
    assert p.kind == "compound"


def calls(p):
    return [(a.name, a.args) for a in p.rule_actions]


def test_demo_sentence_runs_both_halves():
    p = plan("decrease the temperature to 20 deg C and find some shopping centre nearby within time and distance")
    assert p.kind == "compound"
    assert calls(p) == [("set_climate", {"temperature_f": 68}),
                        ("shop_find_places", {"category": "store", "query": "shopping centre"})]


def test_place_rule_pharmacy_and_fallback_tool():
    assert calls(plan("make it 21 degrees and find a pharmacy")) == [
        ("set_climate", {"temperature_f": 70}), ("shop_find_places", {"category": "pharmacy"})]
    p = plan("make it 21 degrees and find a pharmacy", schemas=[_schema("find_places", "query")])
    assert calls(p)[1] == ("find_places", {"query": "pharmacy"})
    assert plan("make it 21 degrees and find a pharmacy", schemas=[]).rule_actions[0].name == "set_climate"
    assert len(plan("make it 21 degrees and find a pharmacy", schemas=[]).rule_actions) == 1


def test_place_rule_only_inside_compound():
    assert plan("find a pharmacy nearby").rule_actions == []
    p = plan("I want coffee and a sandwich")
    assert p.kind == "chat" and p.rule_actions == []


def test_place_params_follow_schema():
    schemas = [_schema("shop_find_places", "category", "within_min", "radius_km")]
    p = plan("set it to 21 degrees and find a coffee shop within 15 minutes within 3 km", schemas=schemas)
    assert calls(p)[1] == ("shop_find_places", {"category": "coffee", "within_min": 15, "radius_km": 3.0})


def test_climate_rule_wider_phrasings():
    for text, f in [("make it 21 degrees", 70), ("set 23 degrees", 73), ("22 degrees please", 72),
                    ("set the AC to 22", 72), ("bring it down to 20", 68)]:
        assert plan(text).rule_actions[0].args == {"temperature_f": f}, text
    assert plan("set the AC to 3").rule_actions == []
    assert plan("it is 25 degrees outside").rule_actions == []
    assert plan("make it 50 degrees").rule_actions == []


def test_celsius_explicit_and_imperial_default():
    assert plan("set the temperature to 20 deg C", metric=False).rule_actions[0].args == {"temperature_f": 68}
    assert plan("set the AC to 70 degrees", metric=False).rule_actions[0].args == {"temperature_f": 70}


def test_relative_and_chat():
    assert plan("make it cooler by 2 degrees", temp_f=72).rule_actions[0].args == {"temperature_f": 68}
    p = plan("what is the weather like")
    assert p.kind == "chat" and p.rule_actions == []
    assert plan("set it to 20 degrees").kind == "tool"


def test_ledger_normalises():
    led = ActionLedger()
    led.record("set_climate", {"temperature_f": 68})
    assert led.seen("set_climate", {"temperature_f": 68.0, "fan": None})
    assert led.seen("set_climate", {"temperature_c": 20})
    assert not led.seen("set_climate", {"temperature_f": 70})
    led.clear()
    assert not led.seen("set_climate", {"temperature_f": 68})


class FakeLLM:
    label, model = "fake", "fake"

    def __init__(self):
        self.round = 0
        self.systems = []

    async def stream(self, messages, tools):
        self.systems.append(messages[0]["content"])
        self.round += 1
        if self.round == 1:
            yield "tool_calls", [{"id": "c1", "name": "set_climate", "arguments": json.dumps({"temperature_f": 68})}]
        else:
            yield "text", "Done, it is 68 degrees."


def test_ledger_prevents_double_execution():
    async def go():
        cfg = Settings.load(ROOT / "tests" / "empty.env")
        cfg.raw["UNITS"] = "metric"
        eng = Engine(cfg, lambda k, v: None, Path.home() / ".cache" / "nova-cabin" / "test", sim=True)
        eng.messages = []
        eng.llm = FakeLLM()
        eng.tools = SimpleNamespace(schemas=[], captions={}, run=None)
        runs = []

        async def run(name, args):
            runs.append((name, args))
            return {"ok": True}, None, None
        eng.tools.run = run
        eng.planner = Planner(eng)

        async def speak(q):
            while await q.get() is not None:
                pass
        eng._speak_loop = speak
        await eng._respond("set it to 20 degrees")
        return eng, runs

    eng, runs = asyncio.run(go())
    assert runs == [("set_climate", {"temperature_f": 68})]
    assert "Already done this turn" in eng.llm.systems[0]


def test_fan_level_is_not_temperature():
    assert plan("turn the AC to 3 and find parking").rule_actions == []
    assert plan("set the ac fan to 3").rule_actions == []
    assert plan("set the seat heater to 20 degrees").rule_actions == []


def test_out_of_range_temperature_ignored():
    assert plan("set the temperature to 150").rule_actions == []
    assert plan("set the temperature to 5 degrees").rule_actions == []
    assert plan("set the temperature to 72 degrees F").rule_actions[0].args == {"temperature_f": 72}


def test_weak_verbs_do_not_make_compound():
    assert plan("find a bar and grill that is open late").kind == "chat"


def test_rule_action_error_is_reported_not_ledgered():
    async def go():
        cfg = Settings.load(ROOT / "tests" / "empty.env")
        cfg.raw["UNITS"] = "metric"
        eng = Engine(cfg, lambda k, v: None, Path.home() / ".cache" / "nova-cabin" / "test", sim=True)
        eng.messages = []
        eng.llm = FakeLLM()
        runs = []

        async def run(name, args):
            runs.append((name, args))
            return ({"error": "climate unavailable"} if len(runs) == 1 else {"ok": True}), None, None
        eng.tools = SimpleNamespace(schemas=[], captions={}, run=run)
        eng.planner = Planner(eng)

        async def speak(q):
            while await q.get() is not None:
                pass
        eng._speak_loop = speak
        await eng._respond("set it to 20 degrees")
        return eng, runs

    eng, runs = asyncio.run(go())
    assert "failed" in eng.llm.systems[0] and "climate unavailable" in eng.llm.systems[0]
    assert "Already done" not in eng.llm.systems[0]
    assert len(runs) == 2
    assert eng.planner.ledger.seen("set_climate", {"temperature_f": 68})


def test_engine_stop_awaits_closers():
    async def go():
        eng = Engine(Settings.load(ROOT / "tests" / "empty.env"), lambda k, v: None,
                     Path.home() / ".cache" / "nova-cabin" / "test", sim=True)
        order = []

        async def a():
            order.append("a")

        async def bad():
            raise RuntimeError("boom")

        async def b():
            order.append("b")
        eng.closers[:] = [a, bad, b]
        closed = SimpleNamespace(close=lambda: None, flush=lambda: None)
        eng.mic, eng.player = closed, closed

        async def aclose():
            order.append("http")
        eng.http = SimpleNamespace(aclose=aclose)
        await eng.stop()
        return order
    assert asyncio.run(go()) == ["b", "a", "http"]


class RepeatSearchLLM:
    label, model = "fake", "fake"

    def __init__(self, args):
        self.round, self.args = 0, args

    async def stream(self, messages, tools):
        self.round += 1
        if self.round == 1:
            yield "tool_calls", [{"id": "c1", "name": "shop_find_places", "arguments": json.dumps(self.args)}]
        else:
            yield "text", "Here are some options."


def test_ledger_skips_model_repeat_of_rule_search():
    async def go():
        cfg = Settings.load(ROOT / "tests" / "empty.env")
        cfg.raw["UNITS"] = "metric"
        eng = Engine(cfg, lambda k, v: None, Path.home() / ".cache" / "nova-cabin" / "test", sim=True)
        eng.messages = []
        eng.llm = RepeatSearchLLM({"category": "pharmacy"})
        runs = []

        async def run(name, args):
            runs.append((name, args))
            return {"ok": True}, None, None
        eng.tools = SimpleNamespace(schemas=SHOP_SCHEMAS, captions={}, run=run)
        eng.planner = Planner(eng)

        async def speak(q):
            while await q.get() is not None:
                pass
        eng._speak_loop = speak
        await eng._respond("make it 21 degrees and find a pharmacy")
        return runs

    assert asyncio.run(go()) == [("set_climate", {"temperature_f": 70}), ("shop_find_places", {"category": "pharmacy"})]


def test_statements_do_not_set_the_cabin():
    assert "set_climate" not in [a.name for a in plan("it's 25 degrees and find a pharmacy").rule_actions]
    assert "set_climate" not in [a.name for a in plan("it is 30 degrees, find me a mall").rule_actions]
    assert plan("it's 25 degrees outside").rule_actions == []
    assert calls(plan("make it 25 degrees and find a pharmacy"))[0] == ("set_climate", {"temperature_f": 77})


def test_split_keeps_separator_text():
    assert Planner.split("set it to 20 degrees, then find a mall within time, and distance") == [
        "set it to 20 degrees", "find a mall within time, and distance"]


def test_place_search_keys_unify_across_tools():
    led = ActionLedger()
    led.record("shop_find_places", {"category": "store", "query": "shopping centre within 10 minutes"})
    assert led.seen("find_places", {"query": "shopping centre"})
    assert led.seen("shop_find_places", {"category": "store", "query": "shopping centre"})
    assert not led.seen("find_places", {"query": "pharmacy"})
    assert not led.seen("find_places", {"query": "bakery"})


def test_specific_query_words_keep_keys_apart():
    led = ActionLedger()
    led.record("find_places", {"query": "pizza restaurant"})
    assert not led.seen("find_places", {"query": "sushi restaurant"})
    assert led.seen("find_places", {"query": "a pizza restaurant nearby"})
    assert led.seen("shop_find_places", {"category": "food", "query": "pizza"})
    led.record("find_places", {"query": "medical shop"})
    assert led.seen("shop_find_places", {"category": "pharmacy"})


def test_distinct_model_search_after_rule_is_not_skipped():
    async def go(args, name):
        cfg = Settings.load(ROOT / "tests" / "empty.env")
        eng = Engine(cfg, lambda k, v: None, Path.home() / ".cache" / "nova-cabin" / "test", sim=True)
        eng.messages = []
        llm = RepeatSearchLLM(args)
        orig = llm.stream

        async def stream(messages, tools):
            async for kind, val in orig(messages, tools):
                if kind == "tool_calls":
                    val[0]["name"] = name
                yield kind, val
        llm.stream = stream
        eng.llm = llm
        runs = []

        async def run(n, a):
            runs.append((n, a))
            return {"ok": True}, None, None
        eng.tools = SimpleNamespace(schemas=SHOP_SCHEMAS, captions={}, run=run)
        eng.planner = Planner(eng)

        async def speak(q):
            while await q.get() is not None:
                pass
        eng._speak_loop = speak
        await eng._respond("find a pharmacy and find a bakery")
        return [n for n, _ in runs], runs
    _, runs = asyncio.run(go({"category": "bakery"}, "shop_find_places"))
    assert ("shop_find_places", {"category": "bakery"}) in runs and len(runs) >= 2


def test_model_find_places_after_rule_search_runs_once():
    async def go():
        cfg = Settings.load(ROOT / "tests" / "empty.env")
        cfg.raw["UNITS"] = "metric"
        eng = Engine(cfg, lambda k, v: None, Path.home() / ".cache" / "nova-cabin" / "test", sim=True)
        eng.messages = []
        llm = RepeatSearchLLM({"query": "shopping centre"})
        orig = llm.stream

        async def stream(messages, tools):
            async for kind, val in orig(messages, tools):
                if kind == "tool_calls":
                    val[0]["name"] = "find_places"
                yield kind, val
        llm.stream = stream
        eng.llm = llm
        runs = []

        async def run(name, args):
            runs.append((name, args))
            return {"ok": True}, None, None
        eng.tools = SimpleNamespace(schemas=SHOP_SCHEMAS, captions={}, run=run)
        eng.planner = Planner(eng)

        async def speak(q):
            while await q.get() is not None:
                pass
        eng._speak_loop = speak
        await eng._respond("set it to 20 degrees and find a shopping centre")
        return runs

    runs = asyncio.run(go())
    assert [n for n, _ in runs] == ["set_climate", "shop_find_places"]
