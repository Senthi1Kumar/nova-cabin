"""Tool-calling eval, offline.

The offline bar is a regression guard on the sim brain and the rules, not a measure of model quality; the
online run (tools/eval_tools.py --online) is the model measure.
"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("eval_tools", ROOT / "tools" / "eval_tools.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)


def test_phrase_set_shape():
    items = ev.load_phrases()
    groups = [i["group"] for i in items]
    assert len(items) == 80
    assert {g: groups.count(g) for g in set(groups)} == {"single": 30, "compound": 30, "ambiguous": 10,
                                                          "out_of_scope": 10}
    assert len({i["id"] for i in items}) == 80
    assert any("20 deg C" in i["phrase"] and "shopping centre" in i["phrase"] for i in items)


def test_every_option_constrains_search_tools():
    for item in ev.load_phrases():
        for opt in ev.options(item):
            for c in opt:
                if c["tool"] in ("find_places", "web_search", "shop_find_places"):
                    assert c.get("args"), f"{item['id']}: bare {c['tool']} would match any search"


def test_judge_constraints():
    item = {"expected_calls": [{"tool": "set_climate", "args": {"temperature_f": 68}},
                               {"tool": "find_places", "args": {"query": {"contains": ["mall"]}}}]}
    ok = [{"tool": "set_climate", "args": {"temperature_f": 68}}, {"tool": "find_places", "args": {"query": "Mall"}}]
    assert ev.judge(item, ok)[0]
    assert not ev.judge(item, [dict(ok[0], args={"temperature_f": 20}), ok[1]])[0]
    assert not ev.judge(item, ok[:1])[0]
    assert ev.judge({"expected_calls": []}, [])[0]
    assert not ev.judge({"expected_calls": []}, ok[:1])[0]


def test_unregistered_tool_is_skipped(monkeypatch):
    items = [{"id": "x1", "group": "single", "phrase": "what time is it",
              "expected_calls": [{"tool": "no_such_tool", "args": {}}]}]
    monkeypatch.setattr(ev, "load_phrases", lambda path=ev.PHRASES: items)
    res = asyncio.run(ev.evaluate(sim=True))
    assert res["skipped"] == 1 and res["scored"] == 0


def test_eval_offline_bar():
    res = asyncio.run(ev.offline_accuracy())
    fails = [f"{r['id']} {r['phrase']}: {r['detail']}" for r in res["rows"] if r["status"] == "fail"]
    assert res["accuracy"] >= 0.95, json.dumps(fails, indent=1)


def test_evaluate_leaves_user_data_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NOVA_CABIN_DATA", raising=False)
    res = asyncio.run(ev.evaluate(sim=True, kind="oos"))
    assert res["scored"] == 10
    assert not (tmp_path / ".local").exists()
    assert "NOVA_CABIN_DATA" not in os.environ


def test_kind_and_ids_filters():
    res = asyncio.run(ev.evaluate(sim=True, only=["s05", "c01"]))
    assert [r["id"] for r in res["rows"]] == ["s05", "c01"]
    assert {"raw_accuracy", "accuracy"} <= set(res)
