"""Tool-calling eval: phrases in tests/eval/tool_calls.jsonl against the planner plus a brain.

    python tools/eval_tools.py --offline            # sim brain + rules, no network
    python tools/eval_tools.py --online             # the model configured in .env, one fresh engine per phrase
    python tools/eval_tools.py --offline --kind compound --verbose
    python tools/eval_tools.py --offline --ids c01,c02

Every call that reaches ``engine.tools.run`` is recorded, whether a rule or the model issued it. A phrase passes
when every expected call is matched by a recorded call (extra calls are reported, and fail only out-of-scope
phrases, which must make none); ``alternatives`` lists other acceptable call sets. Phrases whose expected tool is
not registered are skipped. Results are written to eval_results/<date>.json.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cabin.config import Settings
from cabin.engine import Engine

PHRASES = ROOT / "tests" / "eval" / "tool_calls.jsonl"
OUT_DIR = ROOT / "eval_results"


def load_phrases(path: Path = PHRASES) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _arg_ok(want, got) -> bool:
    if isinstance(want, dict):
        if got is None:
            return False
        if "contains" in want:
            words = want["contains"] if isinstance(want["contains"], list) else [want["contains"]]
            return any(str(w).lower() in str(got).lower() for w in words)
        try:
            return ("gte" not in want or float(got) >= want["gte"]) and ("lte" not in want or float(got) <= want["lte"])
        except (TypeError, ValueError):
            return False
    if isinstance(want, bool) or isinstance(got, bool):
        return want is got
    if isinstance(want, (int, float)):
        try:
            return abs(float(got) - float(want)) < 0.5
        except (TypeError, ValueError):
            return False
    return str(got).lower() == str(want).lower()


def call_matches(expected: dict, actual: dict) -> bool:
    if expected["tool"] != actual["tool"]:
        return False
    args = actual.get("args") or {}
    return all(_arg_ok(w, args.get(k)) for k, w in (expected.get("args") or {}).items())


def options(item: dict) -> list[list[dict]]:
    return [item["expected_calls"]] + list(item.get("alternatives") or [])


def judge(item: dict, recorded: list[dict], primary_only: bool = False) -> tuple[bool, str]:
    opts = options(item)[:1] if primary_only else options(item)
    if not item["expected_calls"]:
        return (not recorded, "" if not recorded else "unexpected calls: " + _fmt(recorded))
    best = None
    for opt in opts:
        missing = [e for e in opt if not any(call_matches(e, a) for a in recorded)]
        if not missing:
            return True, ""
        if best is None or len(missing) < len(best):
            best = missing
    return False, "missing: " + _fmt(best or []) + " | got: " + (_fmt(recorded) or "no calls")


def _fmt(calls: list[dict]) -> str:
    return "; ".join(f"{c['tool']}({json.dumps(c.get('args') or {}, ensure_ascii=False)})" for c in calls)


def expected_tools(item: dict) -> set[str]:
    return {c["tool"] for opt in options(item) for c in opt}


def required_tools(item: dict) -> set[str]:
    """Tools of the first option; an alternative may cover for a missing one."""
    return {c["tool"] for c in item["expected_calls"]}


async def new_engine(cfg: Settings, sim: bool) -> Engine:
    eng = Engine(cfg, lambda k, v: None, Path(tempfile.mkdtemp(prefix="eval-", dir=os.environ.get("NOVA_CABIN_DATA") or _scratch())), sim=sim)
    await eng.start()
    eng.tts = None                                                   # replies are not synthesised or played

    async def no_playback(start: float, end: float) -> None:
        return

    eng._level_until = no_playback
    return eng


async def drive(eng: Engine, phrase: str, cap: float, stub_writes: bool = False) -> dict:
    """One turn on ``eng``; returns every distinct tool call that reached ``eng.tools.run``."""
    eng.messages.clear()
    recorded: list[dict] = []
    marks: list[float] = []
    real_run = eng.tools.run

    async def spy(name: str, args: dict):
        call = {"tool": name, "args": args}
        if call not in recorded:
            recorded.append(call)
            marks.append(time.monotonic())
        if stub_writes and (name == "shop_request_checkout" or (name.startswith("pay_") and name != "pay_list_cards")):
            return {"ok": True, "stubbed": True}, None, None
        return await real_run(name, args)

    eng.tools.run = spy
    t0 = time.monotonic()
    error = ""
    try:
        await asyncio.wait_for(eng._respond(phrase), cap)
    except TimeoutError:
        error = f"timeout after {cap:.0f}s"
    except Exception as e:                                           # noqa: BLE001
        error = f"{type(e).__name__}: {e}"[:200]
    finally:
        eng.tools.run = real_run
    latency = (marks[0] if marks else time.monotonic()) - t0
    return {"calls": recorded, "latency_s": round(latency, 3), "error": error}


async def run_phrase(cfg: Settings, phrase: str, sim: bool, cap: float, stub_writes: bool = False) -> dict:
    eng = await new_engine(cfg, sim)
    try:
        return await drive(eng, phrase, cap, stub_writes)
    finally:
        with contextlib.suppress(Exception):
            await eng.stop()


def _scratch() -> str:
    d = Path.home() / ".cache" / "nova-cabin"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def make_settings(sim: bool) -> Settings:
    cfg = Settings.load(ROOT / ".env")
    cfg.raw["UNITS"] = "metric"
    cfg.raw["SIMULATION"] = "true" if sim else "false"
    cfg.raw["SIM_VOICE"] = "silent"
    return cfg


async def _registered(cfg: Settings, sim: bool) -> set[str]:
    eng = await new_engine(cfg, sim)
    try:
        return {s["function"]["name"] for s in eng.tools.schemas}
    finally:
        await eng.stop()


async def _run_all(cfg: Settings, sim: bool, items: list[dict], cap: float, workers: int) -> list[dict]:
    """Offline phrases share a small pool of engines; online each phrase gets a fresh engine."""
    results: dict[str, dict] = {}
    if not sim:
        for item in items:
            results[item["id"]] = await run_phrase(cfg, item["phrase"], sim, cap, stub_writes=True)
        return [results[i["id"]] for i in items]
    queue: asyncio.Queue = asyncio.Queue()
    for item in items:
        queue.put_nowait(item)

    async def worker() -> None:
        eng = await new_engine(cfg, sim)
        try:
            while not queue.empty():
                item = queue.get_nowait()
                results[item["id"]] = await drive(eng, item["phrase"], cap)
        finally:
            await eng.stop()

    await asyncio.gather(*(worker() for _ in range(max(1, min(workers, len(items))))))
    return [results[i["id"]] for i in items]


KINDS = {"single": "single", "compound": "compound", "ambiguous": "ambiguous", "oos": "out_of_scope",
         "out_of_scope": "out_of_scope"}


async def evaluate(sim: bool, kind: str | None = None, cap: float = 30.0, only: list[str] | None = None,
                   workers: int = 8) -> dict:
    """Runs with app data redirected to a scratch directory that is removed afterwards."""
    data = tempfile.mkdtemp(prefix="nova-eval-", dir=_scratch())
    previous = os.environ.get("NOVA_CABIN_DATA")
    os.environ["NOVA_CABIN_DATA"] = data
    try:
        return await _evaluate(sim, kind, cap, only, workers)
    finally:
        if previous is None:
            os.environ.pop("NOVA_CABIN_DATA", None)
        else:
            os.environ["NOVA_CABIN_DATA"] = previous
        shutil.rmtree(data, ignore_errors=True)


async def _evaluate(sim: bool, kind: str | None, cap: float, only: list[str] | None, workers: int) -> dict:
    group = KINDS[kind] if kind else None
    items = [i for i in load_phrases() if (not group or i["group"] == group) and (not only or i["id"] in only)]
    cfg = make_settings(sim)
    registered = await _registered(cfg, sim)
    runnable = [i for i in items if required_tools(i) <= registered]
    ran = dict(zip((i["id"] for i in runnable), await _run_all(cfg, sim, runnable, cap, workers))) if runnable else {}
    rows = []
    for item in items:
        if item["id"] not in ran:
            rows.append({**item, "status": "skipped", "calls": [], "latency_s": 0.0,
                         "detail": "tool not registered: " + ", ".join(sorted(required_tools(item) - registered))})
            continue
        r = ran[item["id"]]
        ok, detail = judge(item, r["calls"])
        raw_ok, _ = judge(item, r["calls"], primary_only=True)
        if r["error"] and not ok:
            detail = f"{r['error']} | {detail}"
        rows.append({**item, "status": "pass" if ok else "fail", "raw_status": "pass" if raw_ok else "fail",
                     "calls": r["calls"], "latency_s": r["latency_s"], "detail": detail})
    scored = [r for r in rows if r["status"] != "skipped"]
    passed = sum(r["status"] == "pass" for r in scored)
    raw_passed = sum(r["raw_status"] == "pass" for r in scored)
    lat = [r["latency_s"] for r in scored if r["calls"]]
    by_group = {}
    for g in sorted({r["group"] for r in scored}):
        gs = [r for r in scored if r["group"] == g]
        by_group[g] = round(sum(r["status"] == "pass" for r in gs) / len(gs), 3)
    return {"mode": "offline" if sim else "online", "model": _model_name(cfg, sim),
            "date": dt.datetime.now().isoformat(timespec="seconds"), "total": len(rows), "scored": len(scored),
            "skipped": len(rows) - len(scored), "passed": passed,
            "accuracy": round(passed / len(scored), 4) if scored else 0.0,
            "raw_passed": raw_passed, "raw_accuracy": round(raw_passed / len(scored), 4) if scored else 0.0,
            "by_group": by_group,
            "mean_latency_s": round(sum(lat) / len(lat), 3) if lat else 0.0, "rows": rows}


def _model_name(cfg: Settings, sim: bool) -> str:
    if sim:
        return "sim-brain"
    conf = cfg.llm()
    return f"{conf['provider']}:{conf['model']}" if conf else "none"


async def offline_accuracy() -> dict:
    return await evaluate(sim=True)


def report(res: dict, verbose: bool = False) -> None:
    for r in res["rows"]:
        if r["status"] == "pass" and not verbose:
            continue
        print(f"[{r['status']:<7}] {r['id']} ({r['group']}) {r['phrase']}")
        if r["status"] != "pass":
            print(f"          {r['detail']}")
        elif verbose:
            print(f"          {_fmt(r['calls'])}")
    print(f"\nmode={res['mode']} model={res['model']}")
    print(f"accuracy {res['accuracy']:.3f}  ({res['passed']}/{res['scored']} scored, {res['skipped']} skipped; "
          f"widened rubric with alternatives)")
    print(f"raw accuracy {res['raw_accuracy']:.3f}  ({res['raw_passed']}/{res['scored']}, primary expected call only)")
    print("by group: " + ", ".join(f"{g} {v:.2f}" for g, v in res["by_group"].items()))
    print(f"mean time to first tool call: {res['mean_latency_s']:.2f}s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--online", action="store_true")
    ap.add_argument("--kind", choices=sorted(KINDS))
    ap.add_argument("--ids", help="comma-separated phrase ids, e.g. c01,c02")
    ap.add_argument("--cap", type=float, default=30.0, help="seconds allowed per phrase")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args()
    logging.disable(logging.CRITICAL)                                # library logs can carry request URLs with keys
    only = [i.strip() for i in a.ids.split(",") if i.strip()] if a.ids else None
    scrubbed = io.StringIO()                                         # stray library stderr can carry request URLs
    with contextlib.redirect_stderr(scrubbed):
        res = asyncio.run(evaluate(sim=a.offline, kind=a.kind, cap=a.cap, only=only))
    report(res, a.verbose)
    if not a.no_write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUT_DIR / f"{dt.date.today().isoformat()}-{res['mode']}.json"
        path.write_text(json.dumps(res, indent=1, ensure_ascii=False))
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
