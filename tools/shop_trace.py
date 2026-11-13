"""Headless commerce + payment run through the real engine with the simulated providers.

    python tools/shop_trace.py            # find coffee → open → add → checkout → approve → OTP → paid

No Qt window, no keys, no network. Prints the event stream and exits non-zero if the order is not captured.
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("NOVA_CABIN_DATA", tempfile.mkdtemp())
os.environ.setdefault("PAY_SIM_STEP_S", "0.2")
from cabin.config import Settings
from cabin.engine import Engine

T0 = time.monotonic()
QUIET = {"level", "words", "lips_stop", "reply", "vehicle", "providers"}
events: list[tuple[str, object]] = []


def emit(k, v):
    events.append((k, v))
    if k in QUIET:
        return
    if k == "card" and v:
        v = f"[{v['kind']}] {v.get('title', '')} | " + " | ".join(str(r.get("value", "")) for r in v.get("rows", []))
    if k == "checkout" and v:
        v = {x: v.get(x) for x in ("stage", "method", "total", "otp_pending")}
    print(f"{time.monotonic() - T0:6.1f}s  {k:<9} {v}", flush=True)


async def tool(eng, name, **args):
    res, card, _nav = await eng.tools.run(name, args)
    if card:
        emit("card", card)
    print(f"{time.monotonic() - T0:6.1f}s  tool      {name} -> {str(res)[:160]}", flush=True)
    return res


async def settle(eng, secs=6.0):
    t = time.monotonic()
    while time.monotonic() - t < secs and eng.state != "idle":
        await asyncio.sleep(0.1)


async def main():
    cfg = Settings.load(Path(__file__).resolve().parents[1] / ".env")
    cfg.raw["SIM_VOICE"] = "murmur"
    eng = Engine(cfg, emit, Path(tempfile.mkdtemp()), sim=True, autopilot=False)
    await eng.start()
    names = sorted(s["function"]["name"] for s in eng.tools.schemas)
    print("tools:", ", ".join(names))
    res = await tool(eng, "shop_find_places", category="coffee")
    places = res.get("places") or res.get("results") or []
    assert places, res
    pid = places[0]["place_id"] if "place_id" in places[0] else places[0]["id"]
    res = await tool(eng, "shop_open_place", place_id=pid)
    items = [i for s in res.get("menu", []) for i in s.get("items", [])]
    assert items, res
    iid = items[0]["item_id"] if "item_id" in items[0] else items[0]["id"]
    await tool(eng, "shop_add_to_cart", item_id=iid, qty=2)
    await tool(eng, "shop_set_qty", item_id=iid, qty=1)
    await tool(eng, "shop_view_cart")
    await tool(eng, "shop_request_checkout")
    await settle(eng)
    if hasattr(eng.stt, "script"):
        eng.stt.script = [""]                 # the sim STT must not inject tour requests while the ladder listens
    print("-- driver: 'pay with my mastercard'")
    assert await eng._intercept("pay with my mastercard")
    print("-- driver: 'approve'")
    assert await eng._intercept("approve")
    for _ in range(100):                       # wait for the ladder (sim) to finish or ask for a code
        await asyncio.sleep(0.1)
        last = next((v for k, v in reversed(events) if k == "checkout"), {})
        if last.get("stage") in ("done", "declined", "cancelled") or last.get("otp_pending"):
            break
    last = next((v for k, v in reversed(events) if k == "checkout"), {})
    if last.get("otp_pending"):
        code = getattr(getattr(eng.payment, "co", None), "phone_note", None)
        print("-- driver speaks the code", "(known)" if code else "(unknown: trying 123456)")
        assert await eng._intercept(str(code or "123456"))
        for _ in range(200):
            await asyncio.sleep(0.1)
            last = next((v for k, v in reversed(events) if k == "checkout"), {})
            if last.get("stage") in ("done", "declined", "cancelled"):
                break
    await settle(eng)
    print("final stage:", last.get("stage"))
    await eng.stop()
    return 0 if last.get("stage") == "done" else 1


sys.exit(asyncio.run(main()))
