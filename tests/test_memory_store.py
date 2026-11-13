import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

from cabin.brain import Planner
from cabin.config import Settings
from cabin.engine import Engine
from cabin.memory import Memory
from cabin.memory.store import SessionStore


def _call(i, name="get_weather", args="{}"):
    return {"id": i, "name": name, "arguments": args}


def test_record_and_load_roundtrip(tmp_path):
    s = SessionStore(tmp_path / "m.db")
    sess = s.open_session("d1")
    s.record_turn(sess.id, "user", "weather in Reno?", [], [], [])
    s.record_turn(sess.id, "assistant", None, [_call("abc")], [{"id": "abc", "content": '{"t":5}'}],
                  [{"kind": "weather"}])
    s.record_turn(sess.id, "assistant", "Five degrees.", [], [], [])
    msgs = SessionStore(tmp_path / "m.db").load_messages(sess.id)
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]
    assert msgs[1]["tool_calls"][0]["id"] == "call_1" == msgs[2]["tool_call_id"]
    assert msgs[1]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert msgs[2]["content"] == '{"t":5}' and msgs[3]["content"] == "Five degrees."
    assert s.last_session("d1").id == sess.id and s.last_session("other") is None


def test_load_never_starts_with_orphan_tool_message(tmp_path):
    s = SessionStore(tmp_path / "m.db")
    sess = s.open_session("d")
    for i in range(6):
        s.record_turn(sess.id, "user", f"question {i} " + "x" * 400)
        s.record_turn(sess.id, "assistant", None, [_call(f"id{i}")], [{"id": f"id{i}", "content": "r" * 400}])
        s.record_turn(sess.id, "assistant", f"answer {i}")
    msgs = s.load_messages(sess.id, budget_tokens=700)
    assert msgs and msgs[0]["role"] == "user" and msgs[0]["content"].startswith("question")
    assert len(msgs) < 18
    ids = [t["id"] for m in msgs for t in m.get("tool_calls", [])]
    assert all(any(m.get("tool_call_id") == i for m in msgs) for i in ids)


def test_unanswered_tool_calls_are_dropped(tmp_path):
    s = SessionStore(tmp_path / "m.db")
    sess = s.open_session("d")
    s.record_turn(sess.id, "user", "go")
    s.record_turn(sess.id, "assistant", "(interrupted by the driver)", [_call("a")], [])
    msgs = s.load_messages(sess.id)
    assert msgs == [{"role": "user", "content": "go"},
                    {"role": "assistant", "content": "(interrupted by the driver)"}]


class _Eng:
    HISTORY = 18

    def __init__(self):
        self.messages, self.system_extra, self.intercepts, self.closers = [], [], [], []
        self.last_card, self.llm = None, None
        self.spoken = []


def test_memory_survives_restart_and_tool_ids_renumbered(tmp_path):
    eng = _Eng()
    m = Memory(eng, tmp_path / "m.db", driver_id="d")
    eng.messages = [{"role": "user", "content": "hi"},
                    {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "toolu_9", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "toolu_9", "content": "ok"},
                    {"role": "assistant", "content": "done"}]
    m.record_turn(0, {"kind": "x"})
    eng2 = _Eng()
    m2 = Memory(eng2, tmp_path / "m.db", driver_id="d")
    assert m2.session.id == m.session.id
    assert eng2.messages[1]["tool_calls"][0]["id"] == "call_1"
    assert [x["role"] for x in eng2.messages] == ["user", "assistant", "tool", "assistant"]


def test_stale_session_starts_fresh(tmp_path):
    s = SessionStore(tmp_path / "m.db")
    old = s.open_session("d")
    s.record_turn(old.id, "user", "old")
    with s._tx() as db:
        db.execute("UPDATE sessions SET updated_at=?", (time.time() - 5 * 3600,))
    eng = _Eng()
    m = Memory(eng, tmp_path / "m.db", driver_id="d")
    assert m.session.id != old.id and eng.messages == []


def test_compaction_moves_summary_to_slot_and_survives_reload(tmp_path):
    eng = _Eng()
    m = Memory(eng, tmp_path / "m.db", driver_id="d", budget_tokens=300)
    for i in range(10):
        eng.messages += [{"role": "user", "content": f"q{i} " + "w" * 200},
                         {"role": "assistant", "content": f"a{i} " + "z" * 200}]
        m.record_turn(len(eng.messages) - 2, None)

    async def go():
        return await m.compact(eng.messages)
    out = asyncio.run(go())
    assert all(x["role"] != "system" for x in out) and len(out) < 20
    assert any("Summary of the earlier conversation" in line for line in eng.system_extra)
    eng2 = _Eng()
    Memory(eng2, tmp_path / "m.db", driver_id="d", budget_tokens=3500)
    assert len(eng2.messages) == len(out)
    assert any("Summary of the earlier conversation" in line for line in eng2.system_extra)


def test_memory_intent_is_first_intercept_and_speaks(tmp_path):
    eng = _Eng()
    eng.intercepts.append(lambda t: None)
    m = Memory(eng, tmp_path / "m.db", driver_id="d")
    said = []

    async def speak(text):
        said.append(text)
    m.speak = speak
    assert eng.intercepts[0] == m.intercept or eng.intercepts[0].__func__ is m.intercept.__func__
    assert asyncio.run(eng.intercepts[0]("what do you remember?")) is True
    assert said and "remember" in said[0].lower()
    assert asyncio.run(eng.intercepts[0]("plan a trip")) is False


def test_profile_mining_updates_slot_without_llm_skips(tmp_path):
    eng = _Eng()
    m = Memory(eng, tmp_path / "m.db", driver_id="d")
    asyncio.run(m.mine_now())                      # no llm: nothing happens
    assert json.dumps(eng.system_extra).count("Driver preferences") == 0

    async def fake(_turns):
        return {"name": "Asha", "home_city": "Los Gatos"}
    m.record_text("user", "I am Asha and like jazz")
    asyncio.run(m.mine_now(extract=fake))
    assert any("Driver preferences" in line for line in eng.system_extra)


ROOT = Path(__file__).resolve().parents[1]


def _forget_all(m):
    async def go():
        m.speak = lambda text: asyncio.sleep(0)
        assert await m.intercept("forget everything about me")
        assert await m.intercept("yes")
    asyncio.run(go())


def test_forget_all_purges_transcripts_summary_and_live_history(tmp_path):
    eng = _Eng()
    m = Memory(eng, tmp_path / "m.db", driver_id="d", budget_tokens=300)
    for i in range(10):
        eng.messages += [{"role": "user", "content": f"secret{i} " + "w" * 200},
                         {"role": "assistant", "content": f"a{i} " + "z" * 200}]
        m.record_turn(len(eng.messages) - 2, None)
    asyncio.run(m.compact(eng.messages))
    assert any(eng.system_extra)
    _forget_all(m)
    assert eng.messages == [] and not any("Summary" in x for x in eng.system_extra)
    eng2 = _Eng()
    m2 = Memory(eng2, tmp_path / "m.db", driver_id="d")
    assert "secret" not in json.dumps(eng2.messages)
    assert m2.store.summary(m2.session.id) == "" and not any("Summary" in x for x in eng2.system_extra)


def test_forget_all_then_mine_does_not_resurface_fact(tmp_path):
    eng = _Eng()
    m = Memory(eng, tmp_path / "m.db", driver_id="d")
    m.record_text("user", "I live in Los Gatos")
    seen = []

    async def fake(turns):
        seen.append(turns)
        return {"home_city": "Los Gatos"} if any("Los Gatos" in t["content"] for t in turns) else {}

    async def go():
        m.speak = lambda text: asyncio.sleep(0)
        await m.intercept("forget everything about me")
        await m.intercept("yes")
        m.record_text("user", "play some jazz")
        await m.flush()
        await m.mine_now(extract=fake)
    asyncio.run(go())
    assert not any("Los Gatos" in line for line in eng.system_extra)
    assert all("Los Gatos" not in t["content"] for batch in seen for t in batch)


class _LLM:
    label, model = "fake", "fake"

    def __init__(self, text="Done."):
        self.text, self.hang = text, False

    async def stream(self, messages, tools=None):
        if self.hang:
            await asyncio.sleep(30)
        yield "text", self.text


def _engine(tmp_path, llm, budget=3500):
    cfg = Settings.load(ROOT / "tests" / "empty.env")
    eng = Engine(cfg, lambda k, v: None, tmp_path / "cache", sim=True)
    eng.messages, eng.llm = [], llm
    eng.tools = SimpleNamespace(schemas=[], captions={}, run=None)
    eng.planner = Planner(eng)
    eng.spoken = []

    async def speak(q):
        while (s := await q.get()) is not None:
            eng.spoken.append(s)
            eng._spoken += s
    eng._speak_loop = speak
    eng.memory = Memory(eng, tmp_path / "m.db", driver_id="d", budget_tokens=budget)
    return eng


def test_engine_turn_is_recorded_without_awaiting_the_writer(tmp_path):
    eng = _engine(tmp_path, _LLM("Hello there."))

    async def go():
        await eng._respond("good morning")
        await eng.memory.flush()
    asyncio.run(go())
    msgs = eng.memory.store.load_messages(eng.memory.session.id)
    assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[1]["content"] == "Hello there."


def test_cancelled_turn_is_recorded_with_interrupted_note(tmp_path):
    llm = _LLM()
    llm.hang = True
    eng = _engine(tmp_path, llm)

    async def go():
        t = asyncio.create_task(eng._respond("tell me a story"))
        await asyncio.sleep(0.2)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        await eng.memory.flush()
    asyncio.run(go())
    msgs = eng.memory.store.load_messages(eng.memory.session.id)
    assert msgs[0]["content"] == "tell me a story"
    assert "interrupted by the driver" in msgs[-1]["content"]


def test_turn_after_background_compaction_is_recorded(tmp_path):
    eng = _engine(tmp_path, _LLM("A short summary."), budget=300)

    async def go():
        for i in range(10):
            await eng._respond(f"question {i} " + "w" * 200)
            await eng.memory.flush()
            if eng.memory._compacting:
                await eng.memory._compacting
        await eng._respond("final question")
        await eng.memory.flush()
    asyncio.run(go())
    assert any("Summary of the earlier conversation" in x for x in eng.system_extra)
    assert len(eng.messages) < 21
    store, sid = eng.memory.store, eng.memory.session.id
    assert store.user_turn_count(sid) == 11
    texts = store.texts_since(sid)
    assert any(t["content"] == "final question" for t in texts)


def _purge_case(tmp_path, monkeypatch, said, action):
    from cabin.memory import profile
    eng = _Eng()
    m = Memory(eng, tmp_path / f"{action}.db", driver_id="d")
    eng.messages = [{"role": "user", "content": "private chat"}, {"role": "assistant", "content": "ok"}]
    m.record_turn(0, None)
    m.profile.save(profile.Profile(home_city="Los Gatos", usual_orders=["oat milk"]))
    real = profile.handle_memory_intent

    async def reworded(text, store):
        r = await real(text, store)
        return r and profile.MemoryIntent("Alright then.", r.action)
    monkeypatch.setattr("cabin.memory.handle_memory_intent", reworded)

    async def go():
        m.speak = lambda text: asyncio.sleep(0)
        await m.intercept(said)
        if action == "forget_all":
            await m.intercept("yes")
    asyncio.run(go())
    assert eng.messages == [], action
    assert Memory(_Eng(), tmp_path / f"{action}.db", driver_id="d").engine.messages == []


def test_purge_fires_on_action_not_reply_wording(tmp_path, monkeypatch):
    for said, action in (("forget everything about me", "forget_all"), ("forget my home", "forget_field"),
                         ("forget oat milk", "forget_value")):
        _purge_case(tmp_path, monkeypatch, said, action)


def test_summary_write_in_flight_does_not_land_after_purge(tmp_path):
    eng = _Eng()
    eng.llm = _LLM("late summary")
    m = Memory(eng, tmp_path / "m.db", driver_id="d", budget_tokens=300)
    for i in range(10):
        eng.messages += [{"role": "user", "content": f"q{i} " + "w" * 200},
                         {"role": "assistant", "content": f"a{i} " + "z" * 200}]
        m.record_turn(len(eng.messages) - 2, None)
    gate = asyncio.Event
    old = m.session.id

    async def go():
        ev = gate()
        orig = m._summarise

        async def slow(text):
            await ev.wait()
            return await orig(text)
        m._summarise = slow
        m.speak = lambda text: asyncio.sleep(0)
        m.schedule_compact()
        await asyncio.sleep(0.05)
        await m.intercept("forget everything about me")
        await m.intercept("yes")
        ev.set()
        await asyncio.sleep(0.2)
        m.store.set_summary(old, "stale", 3)
    asyncio.run(go())
    assert not any("Summary" in x for x in eng.system_extra)
    assert m.store.summary(m.session.id) == "" and m.store.summary(old) == ""
    assert eng.messages == []
