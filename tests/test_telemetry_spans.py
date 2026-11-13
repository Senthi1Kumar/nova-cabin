import asyncio
import json
import sqlite3

import pytest

from cabin import telemetry
from cabin.telemetry.spans import Span, TurnSpans


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def adv(self, s):
        self.t += s


def test_turn_spans_sum_to_total():
    c = Clock()
    done = []
    ts = TurnSpans("t1", clock=c, on_done=done.append)
    ts.mark("speech_end"); c.adv(0.12)
    ts.mark("stt_done"); c.adv(0.30)
    ts.mark("first_token"); c.adv(0.50)
    ts.mark("tools_done"); c.adv(0.05)
    ts.mark("tts_first_chunk"); c.adv(0.08)
    ts.mark("first_audio"); c.adv(1.0)
    ts.mark("done")
    rec = done[0]
    assert rec["stt_ms"] == pytest.approx(120)
    assert rec["first_token_ms"] == pytest.approx(300)
    assert rec["tools_ms"] == pytest.approx(500)
    assert rec["tts_first_ms"] == pytest.approx(50)
    assert rec["first_audio_ms"] == pytest.approx(80)
    assert rec["total_ms"] == pytest.approx(2050)
    parts = sum(rec[k] for k in ("stt_ms", "first_token_ms", "tools_ms", "tts_first_ms", "first_audio_ms", "tail_ms"))
    assert parts == pytest.approx(rec["total_ms"])
    assert rec["ttfa_ms"] == pytest.approx(1050)


def test_missing_and_out_of_order_marks_still_sum():
    c = Clock()
    done = []
    ts = TurnSpans("t2", clock=c, on_done=done.append)
    ts.mark("speech_end"); c.adv(0.2)
    ts.mark("first_token"); c.adv(0.1)
    ts.mark("tools_done")  # no stt_done mark at all
    c.adv(0.4)
    ts.mark("first_audio"); c.adv(0.1)
    ts.mark("done")
    ts.mark("done")  # second done is ignored
    assert len(done) == 1
    rec = done[0]
    assert rec["stt_ms"] is None and rec["tts_first_ms"] is None
    parts = sum(rec[k] or 0 for k in ("stt_ms", "first_token_ms", "tools_ms", "tts_first_ms", "first_audio_ms", "tail_ms"))
    assert parts == pytest.approx(rec["total_ms"])


def test_span_start_end():
    c = Clock()
    out = []
    sp = Span("map", clock=c, emit=out.append).start()
    c.adv(0.25)
    sp.end(frames=3)
    assert out[0]["name"] == "map" and out[0]["ms"] == pytest.approx(250) and out[0]["frames"] == 3


@pytest.fixture
def tel(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVA_CABIN_DATA", str(tmp_path))
    telemetry.reset()
    t = telemetry.install(None)
    yield t, tmp_path / "logs"
    telemetry.reset()


def _lines(p):
    return [json.loads(x) for x in p.read_text().splitlines()]


def test_event_routing_and_counters(tel):
    t, logs = tel
    t.event("user", {"text": "navigate to Reno", "partial": False})
    t.event("reply", "Starting navigation.")
    t.event("words", "Starting navigation.")
    t.event("card", {"kind": "route", "title": "Reno", "items": [1, 2, 3]})
    t.event("nav", {"eta_min": 12})
    t.event("checkout", {"stage": "done", "total": 12.5})
    t.event("gps", {"lat": 1.0, "lon": 2.0})
    t.flush()
    conv = _lines(logs / "conversation.jsonl")
    assert [r["role"] for r in conv if r.get("kind") == "text"][:2] == ["user", "assistant"]
    tools = _lines(logs / "tools.jsonl")
    assert {r["event"] for r in tools} == {"card", "nav", "checkout"}
    assert next(r for r in tools if r["event"] == "card")["summary"]["items"] == 3
    assert t.counters()["gps"] == 1 and t.counters()["card"] == 1


def test_tool_llm_turn_records(tel):
    t, logs = tel
    ts = t.turn()
    ts.mark("speech_end")
    t.tool("search", {"q": "coffee", "OPENROUTER_API_KEY": "sk-or-v1-zzzzzzzzzzzzzzzzzzzz"},
           {"ok": True}, 0.25, agent="research")
    t.tool("bad", {}, None, 0.1, error="timeout")
    t.llm("m1", "openrouter", 10, 20, 0.001, 0.3, 1.2)
    ts.mark("done")
    t.flush()
    tools = _lines(logs / "tools.jsonl")
    tl = [r for r in tools if r.get("kind") == "tool"]
    assert tl[0]["duration_ms"] == pytest.approx(250) and tl[0]["agent"] == "research"
    assert "zzzzzzzz" not in (logs / "tools.jsonl").read_text()
    assert tl[1]["error"] == "timeout"
    perf = _lines(logs / "perf.jsonl")
    turn = next(r for r in perf if r["kind"] == "turn")
    assert turn["model"] == "m1" and turn["tokens_in"] == 10 and turn["tokens_out"] == 20
    assert turn["cost"] == pytest.approx(0.001) and tl[0]["turn_id"] == turn["turn_id"]
    t.close()
    db = sqlite3.connect(logs / "telemetry.db")
    assert db.execute("select count(*) from tools").fetchone()[0] == 2
    assert db.execute("select count(*) from turns").fetchone()[0] == 1
    assert db.execute("select model from llm").fetchone() == ("m1",)


def test_logging_handler_and_off_switch(tmp_path, monkeypatch):
    import logging
    monkeypatch.setenv("NOVA_CABIN_DATA", str(tmp_path))
    telemetry.reset()
    t = telemetry.install_logging(None)
    logging.getLogger("x").warning("hello code 998877")
    t.flush()
    text = (tmp_path / "logs" / "app.jsonl").read_text()
    assert "hello" in text and "998877" not in text
    telemetry.reset()
    monkeypatch.setenv("TELEMETRY", "off")
    t = telemetry.install(None)
    t.event("user", {"text": "x"})
    t.tool("a", {}, {}, 0.1)
    assert t.turn() is not None and t.status()["enabled"] is False
    telemetry.reset()


def test_plugin_install_and_close(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from cabin.telemetry import plugin
    monkeypatch.setenv("NOVA_CABIN_DATA", str(tmp_path))
    telemetry.reset()
    eng = SimpleNamespace(cfg=None, closers=[])
    plugin.install(eng)
    assert eng.telemetry is telemetry.get() and len(eng.closers) == 1
    asyncio.run(eng.closers[0]())
    telemetry.reset()


def test_atexit_registered_and_tee_flushes_partial(tmp_path, monkeypatch):
    import atexit
    import sys
    registered = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a: registered.append(fn))
    monkeypatch.setenv("NOVA_CABIN_DATA", str(tmp_path))
    telemetry.reset()
    t = telemetry.install_logging(None)
    assert t.close in registered
    sys.stdout.write("no newline tail")
    t.close()
    text = (tmp_path / "logs" / "app.jsonl").read_text()
    assert "no newline tail" in text
    telemetry.reset()
