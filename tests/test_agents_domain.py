import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

from cabin.agents.base import AgentRequest
from cabin.agents.nav import NavigationAgent
from cabin.agents.plugin import install
from cabin.agents.research import ResearchAgent
from cabin.agents.sales import SalesAgent
from cabin.agents.vehicle import VehicleAgent
from cabin.config import Settings
from cabin.services.mcp_client import McpClient, McpError
from cabin.tools import Toolbox

FAKE = str(Path(__file__).parent / "fake_mcp_server.py")


class _Engine:
    def __init__(self, **raw):
        self.cfg = Settings(dict(raw))
        self.http = object()
        self.tools = Toolbox(self.cfg, None, None, None, None, None)
        self.events = []
        self.nav_open = False
        self.vehicle = SimpleNamespace(
            lat=39.5, lon=-119.8, pos=(39.5, -119.8), cabin_temp_f=72.0, seat_heat=0, fan=3, ac_on=False, fix_state="sim",
            battery_pct=80.0, range_mi=240,
            snapshot=lambda: {"cabin_temp_f": 72.0, "seat_heat": 0, "fan": 3, "ac_on": False, "lat": 39.5,
                              "lon": -119.8, "fix_state": "sim", "battery_pct": 80.0, "range_mi": 240})

    def emit(self, kind, payload=None):
        self.events.append((kind, payload))


def _tool(eng, name, fn):
    eng.tools.extend([], {name: fn})


def run(coro):
    return asyncio.run(coro)


# ── vehicle ───────────────────────────────────────────────────
def test_vehicle_agent_proposes_on_temperature_drop(tmp_path):
    agent = VehicleAgent(_Engine(), db_path=tmp_path / "v.db")
    p = agent.propose({"temp_f": 70}, {"temp_f": 48}, {"seat_heat": 0, "cabin_temp_f": 70, "ac_on": True}, {})
    assert p is not None and p.action == "set_climate" and p.args == {"seat_heat": 2}
    assert agent.propose({"temp_f": 70}, {"temp_f": 48}, {"seat_heat": 0}, {}) is None      # rate limited


def test_vehicle_proposal_rules_and_rate_limit(tmp_path):
    t = [0.0]
    agent = VehicleAgent(_Engine(), db_path=tmp_path / "v.db", clock=lambda: t[0])
    assert agent.propose({"temp_f": 60}, {"temp_f": 55}, {"seat_heat": 0}, {}) is None
    assert agent.propose({"temp_f": 70}, {"temp_f": 48}, {"seat_heat": 1}, {}) is None      # seat already on
    hot = agent.propose({"temp_f": 95}, {"temp_f": 95}, {"cabin_temp_f": 85, "ac_on": False}, {})
    assert hot.kind == "ac" and hot.args["ac_on"] is True and hot.args["temperature_f"] == 72
    t[0] = 100
    assert agent.propose({"temp_f": 60}, {"temp_f": 60, "rain_in_h": 0.5}, {}, {}) is None
    t[0] = 601
    rain = agent.propose({"temp_f": 60}, {"temp_f": 60, "rain_in_h": 0.5}, {}, {})
    assert rain.kind == "windows" and rain.simulated


def test_vehicle_history_and_weather_event(tmp_path):
    async def go():
        eng = _Engine()

        async def forecast(lat, lon, days=1):
            return {"unit": "°F", "now": {"temp": 70}, "days": [{"lo": 48, "rain": 0}]}
        eng.weather = SimpleNamespace(forecast=forecast)
        agent = VehicleAgent(eng, db_path=tmp_path / "v.db")
        agent.log_action("set_climate", {"seat_heat": 2}, "user")
        res = await agent.run(AgentRequest("what did I change", {"action": "history"}))
        assert res.ok and res.data["actions"][0]["args"] == {"seat_heat": 2}
        prop = await agent.check_weather()
        assert prop and eng.events[-1][0] == "proposal" and eng.events[-1][1]["kind"] == "seat_heat"
        status = await agent.run(AgentRequest("status"))
        assert status.data["state"]["seat_heat"] == 0
    run(go())


# ── research ──────────────────────────────────────────────────
def test_research_agent_falls_back_without_mcp():
    async def go():
        eng = _Engine(EXA_MCP_CMD="/nonexistent/exa-mcp")

        async def web_search(query):
            return {"answer": "fallback answer", "results": [{"title": "T", "source": "s"}]}, None, None
        _tool(eng, "web_search", web_search)
        res = await ResearchAgent(eng).search("chip news")
        assert res.ok and res.data["searches"][0]["source"] == "web_search"
        assert "fallback answer" in res.summary
    run(go())


def test_research_agent_uses_mcp_and_caps_deep_searches():
    async def go():
        eng = _Engine(EXA_MCP_CMD=f"{sys.executable} {FAKE}", EXA_API_KEY="k")
        agent = ResearchAgent(eng)
        one = await agent.search("hello")
        assert one.data["searches"][0] == {"source": "exa", "query": "hello", "text": "exa result for hello"}
        calls = []
        orig = agent._one

        async def counting(q):
            calls.append(q)
            return await orig(q)
        agent._one = counting
        deep = await agent.run(AgentRequest("research ev charging", {"deep": True, "queries": list("abcde")}))
        assert deep.ok and len(calls) == 3 and deep.data["searches_run"] == 3
        assert "exa result" in deep.summary                                  # no LLM configured: notes returned
    run(go())


def test_research_deep_synthesis_uses_llm():
    class Fake:
        async def stream(self, messages, tools=None):
            yield ("text", "Two sentences. Done.")
            yield ("done", "stop")

    async def go():
        eng = _Engine(EXA_MCP_CMD=f"{sys.executable} {FAKE}")
        agent = ResearchAgent(eng)
        agent._client = Fake()
        agent.llm = lambda: agent._client
        deep = await agent.run(AgentRequest("deep dive on batteries", {"deep": True}))
        assert deep.summary == "Two sentences. Done."
    run(go())


def test_mcp_client_errors():
    async def go():
        try:
            await McpClient(["/nonexistent/cmd"]).call("x", {})
        except McpError:
            pass
        else:
            raise AssertionError("expected McpError")
        res = await McpClient([sys.executable, FAKE]).call("web_search_exa", {"query": "q"})
        assert res["content"][0]["text"] == "exa result for q"
    run(go())


# ── sales ─────────────────────────────────────────────────────
class _Pay:
    def __init__(self, rows):
        self.rows = rows
        self.wallet = SimpleNamespace(
            cards=lambda: [SimpleNamespace(token="a", label="Visa ••4242", brand="Visa", last4="4242"),
                           SimpleNamespace(token="b", label="Mastercard ••5555", brand="Mastercard", last4="5555")],
            default=lambda: SimpleNamespace(token="a"))
        self.ledger = SimpleNamespace(recent_transactions=lambda limit=20: rows[:limit])

    def _active_card(self):
        return SimpleNamespace(token="b")


def test_sales_agent_history_from_ledger():
    async def go():
        eng = _Engine()
        eng.payment = _Pay([{"created_at": "2026-11-10T10:00:00", "merchant_id": "m1", "total_minor": 1250,
                             "currency": "USD", "state": "completed",
                             "snapshot": {"place": {"name": "Blue Cafe"}, "lines": [{"name": "Latte"}]}}])
        agent = SalesAgent(eng)
        res = await agent.run(AgentRequest("what did I buy last", {}))
        assert res.ok and res.data["purchases"] == [{
            "when": "2026-11-10T10:00:00", "merchant": "Blue Cafe", "total": 12.5, "currency": "USD",
            "state": "completed", "items": ["Latte"]}]
        cards = await agent.run(AgentRequest("which card"))
        assert [c["active"] for c in cards.data["cards"]] == [False, True]
    run(go())


def test_sales_agent_reads_ledger_file_read_only(tmp_path):
    db = tmp_path / "ledger.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE transactions (tx_id TEXT, state TEXT, merchant_id TEXT, total_minor INTEGER, "
              "currency TEXT, created_at TEXT, snapshot TEXT)")
    c.execute("INSERT INTO transactions VALUES ('t','completed','m9',500,'USD','2026-11-01',?)",
              (json.dumps({"lines": [{"name": "Tea"}]}),))
    c.commit()
    c.close()
    rows = SalesAgent(_Engine(), ledger_path=db).history()
    assert rows[0]["merchant"] == "m9" and rows[0]["items"] == ["Tea"]


def test_sales_agent_cart_and_adjust_via_tools():
    async def go():
        eng = _Engine()
        seen = []

        async def remove(**kw):
            seen.append(kw)
            return {"ok": True}, None, None
        _tool(eng, "shop_remove", remove)
        snap = {"place": {"name": "Blue Cafe"}, "total": 9.0, "currency": "USD",
                "lines": [{"name": "Latte", "qty": 2, "unit_price": 4.5}]}
        eng.shop = SimpleNamespace(snapshot=lambda: snap)
        agent = SalesAgent(eng)
        res = await agent.run(AgentRequest("what is in my cart"))
        assert "2x Latte" in res.summary and "9.00" in res.summary
        res = await agent.run(AgentRequest("remove it", {"args": {"name": "Latte"}}))
        assert res.ok and seen == [{"name": "Latte"}]
    run(go())


# ── navigation and plugin ─────────────────────────────────────
def test_nav_agent_falls_back_to_tools_and_emits_nav():
    async def go():
        eng = _Engine()

        async def navigate_to(destination):
            return {"navigating_to": destination, "distance": "5 mi", "eta": "9 min"}, None, {"name": destination}
        _tool(eng, "navigate_to", navigate_to)
        res = await NavigationAgent(eng).run(AgentRequest("take me to Reno"))
        assert res.ok and res.data["navigating_to"] == "Reno"
        assert eng.events[-1] == ("nav", {"name": "Reno"}) and eng.nav_open
    run(go())


def test_nav_agent_uses_nav_agent_and_places(monkeypatch):
    import types
    from dataclasses import dataclass, field

    @dataclass
    class FakeNav:
        dest: str
        maneuvers: tuple = ("left",)
        handle: object = field(default_factory=object)

    calls = []

    class FakeNavAgent:
        def __init__(self, engine, mapbox):
            calls.append(("init", mapbox))

        async def start(self, dest):
            calls.append(("start", dest))
            return FakeNav(dest.name)

        def stop(self):
            calls.append(("stop",))

    class FakePlaces:
        async def search(self, category, *, near, along=None, within_min=None, limit=6):
            calls.append(("search", category, near, limit))
            return [SimpleNamespace(name="Reno Arch", lat=39.5, lon=-119.8)]

    nav_mod = types.ModuleType("cabin.nav.agent")
    nav_mod.NavAgent = FakeNavAgent
    monkeypatch.setitem(sys.modules, "cabin.nav.agent", nav_mod)

    async def go():
        eng = _Engine()
        async def build_nav(name, lat, lon, address=""):
            return {"name": name, "address": address, "map": "img", "distance": "5 mi", "duration": "9 min",
                    "eta": "9:00 AM", "arrival": "70%"}
        eng.tools.build_nav = build_nav
        eng.mapbox, eng.places = "mb", FakePlaces()
        agent = NavigationAgent(eng)
        res = await agent.run(AgentRequest("take me to Reno Arch"))
        assert res.ok and res.data["navigating_to"] == "Reno Arch" and res.data["nav"]["dest"] == "Reno Arch"
        kind, ui = eng.events[-1]
        assert kind == "nav" and isinstance(ui, dict) and eng.nav_open
        assert {"name", "distance", "duration", "eta", "map"} <= set(ui) and ui["name"] == "Reno Arch"
        assert ui["dest"] == "Reno Arch" and "handle" not in ui
        json.dumps(ui)
        assert calls[0] == ("init", "mb") and ("search", "Reno Arch", (39.5, -119.8), 1) in calls
        await agent.run(AgentRequest("take me to Reno Arch"))
        assert [c[0] for c in calls].count("init") == 1                       # cached
        stop = await agent.run(AgentRequest("stop", {"action": "stop"}))
        assert stop.ok and ("stop",) in calls and not eng.nav_open
    run(go())


def test_active_card_prefers_public_accessor():
    pay = _Pay([])
    pay.active_card = lambda: SimpleNamespace(token="a")
    assert SalesAgent._active_token(pay) == "a"
    del pay.active_card
    assert SalesAgent._active_token(pay) == "b"                                # private fallback
    del _Pay._active_card
    try:
        assert SalesAgent._active_token(pay) == "a"                            # wallet default
    finally:
        _Pay._active_card = lambda self: SimpleNamespace(token="b")


def test_research_deep_keeps_hits_when_one_query_fails():
    async def go():
        eng = _Engine()

        async def web_search(query):
            if query == "bad":
                return {"error": "boom"}, None, None
            return {"answer": f"ok {query}", "results": []}, None, None
        _tool(eng, "web_search", web_search)
        res = await ResearchAgent(eng).run(AgentRequest("research", {"deep": True, "queries": ["bad", "good"]}))
        assert res.ok and res.data["searches_run"] == 2
    run(go())


def test_mcp_timeout_kills_process_group(tmp_path):
    import os
    import time
    pidfile = tmp_path / "child.pid"

    async def go():
        client = McpClient([sys.executable, str(Path(__file__).parent / "fake_mcp_spawner.py"), str(pidfile)],
                           timeout_s=1.5)
        try:
            await client.call("x", {})
        except McpError as e:
            assert "timed out" in str(e)
    run(go())
    child = int(pidfile.read_text())
    for _ in range(20):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise AssertionError("child process survived")


def test_exa_key_never_logged(caplog):
    async def go():
        eng = _Engine(EXA_MCP_CMD=f"{sys.executable} {FAKE}", EXA_API_KEY="sk-secret-value")
        await ResearchAgent(eng).search("hello")
    with caplog.at_level("DEBUG"):
        run(go())
    assert "sk-secret-value" not in caplog.text


def test_plugin_registers_all_agents():
    eng = _Engine()
    install(eng)
    names = {s["function"]["name"] for s in eng.tools.schemas}
    assert {"agent_nav", "agent_sales", "agent_vehicle", "agent_research", "agent_time"} <= names
