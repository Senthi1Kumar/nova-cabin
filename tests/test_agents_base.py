import asyncio
import json
import time

import pytest

from cabin.agents.base import Agent, AgentRequest, AgentResult
from cabin.agents.registry import AgentRegistry
from cabin.config import Settings
from cabin.tools import Toolbox, gather_tools


class _Engine:
    def __init__(self, **raw):
        self.cfg = Settings(dict(raw))
        self.http = object()
        self.tools = Toolbox(self.cfg, None, None, None, None, None)


class _Sleeper(Agent):
    description = "sleeps"

    def __init__(self, engine, name, delay):
        super().__init__(name, engine)
        self.delay = delay

    async def execute(self, request):
        await asyncio.sleep(self.delay)
        return {"slept": self.delay}, f"{self.name} done"


class _Boom(Agent):
    description = "fails"

    async def execute(self, request):
        raise RuntimeError("kaput")


def test_two_agents_run_in_parallel_with_timeout():
    async def go():
        eng = _Engine(AGENT_TIMEOUT_S="0.3")
        reg = AgentRegistry(eng)
        reg.register(_Sleeper(eng, "slow", 2.0))
        reg.register(_Sleeper(eng, "quick", 0.05))
        calls = [{"id": "1", "name": "agent_slow", "arguments": json.dumps({"request": "x"})},
                 {"id": "2", "name": "agent_quick", "arguments": json.dumps({"request": "y"})}]
        t0 = time.monotonic()
        out = await gather_tools(eng.tools, calls)
        return time.monotonic() - t0, {c["name"]: r[0] for c, r in out}

    elapsed, res = asyncio.run(go())
    assert elapsed < 1.0
    assert res["agent_slow"]["ok"] is False and res["agent_slow"]["error"] == "timeout"
    assert res["agent_quick"]["ok"] is True
    assert res["agent_quick"]["data"] == {"slept": 0.05}
    assert res["agent_quick"]["source"] == "quick"
    assert "T" in res["agent_quick"]["as_of"]


def test_exception_becomes_error_result():
    async def go():
        r = await _Boom("boom", _Engine()).run(AgentRequest("hi"))
        return r

    r = asyncio.run(go())
    assert isinstance(r, AgentResult) and r.ok is False and "kaput" in r.error


def test_registry_exposes_schema_and_caption():
    eng = _Engine()
    reg = AgentRegistry(eng)
    reg.register(_Sleeper(eng, "quick", 0))
    names = [s["function"]["name"] for s in eng.tools.schemas]
    assert "agent_quick" in names
    sch = next(s for s in eng.tools.schemas if s["function"]["name"] == "agent_quick")
    props = sch["function"]["parameters"]["properties"]
    assert set(props) == {"request", "context"}
    assert sch["function"]["parameters"]["required"] == ["request"]
    assert "agent_quick" in eng.tools.captions
    assert reg.get("quick") is not None


def test_llm_is_lazy_and_overrides_model():
    class Fake:
        def __init__(self, http, conf):
            self.conf = conf

    eng = _Engine(LLM_PROVIDER="openrouter", OPENROUTER_API_KEY="k", OPENROUTER_MODEL="base/m")
    a = Agent("r", eng, model="vendor/small")
    a._client_factory = Fake
    c = a.llm()
    assert c.conf["model"] == "vendor/small"
    assert eng.cfg.llm()["model"] == "base/m"
    assert a.llm() is c


def test_llm_without_key_raises():
    with pytest.raises(RuntimeError):
        Agent("r", _Engine(), model="m").llm()


def test_time_agent_end_to_end():
    from cabin.agents import plugin

    async def go():
        eng = _Engine()
        plugin.install(eng)
        return await eng.tools.run("agent_time", {"request": "what time is it"})

    res, card, nav = asyncio.run(go())
    assert res["ok"] is True and res["source"] == "time"
    assert {"iso", "date", "time", "timezone"} <= set(res["data"])
    assert card is None and nav is None
