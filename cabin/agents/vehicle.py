"""Vehicle specialist: cabin state, an action history, and a weather watch that proposes comfort changes."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .base import Agent, AgentRequest, now_iso

log = logging.getLogger("agents.vehicle")

PROPOSAL_GAP_S = 600.0
DROP_F = 10.0
HOT_OUTSIDE_C = 32.0
HOT_CABIN_C = 26.0
AC_TARGET_C = 22.0

_SCHEMA = """CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, action TEXT NOT NULL, args TEXT NOT NULL, source TEXT NOT NULL)"""


def f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


@dataclass
class Proposal:
    kind: str                         # seat_heat | ac | windows
    action: str                       # tool the proposal would run, or "suggest"
    args: dict = field(default_factory=dict)
    reason: str = ""
    simulated: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def weather_to_inputs(wx: dict, unit_metric: bool = False) -> tuple[dict, dict]:
    """Reduce a ``Weather.forecast`` result to the (now, forecast) dicts ``propose`` takes, in °F."""
    def conv(t):
        return None if t is None else (t * 9 / 5 + 32 if unit_metric else float(t))
    days = wx.get("days") or []
    today = days[0] if days else {}
    now = {"temp_f": conv((wx.get("now") or {}).get("temp"))}
    fc = {"temp_f": conv(today.get("lo")), "rain_in_h": today.get("rain_in_h")}
    return now, fc


class VehicleAgent(Agent):
    description = ("Vehicle specialist: cabin temperature, seat heat, fan, A/C and position; recent climate "
                   "actions; and weather-driven comfort proposals. Set context.action to 'status', "
                   "'history', 'check' or 'set' (with context.args for set_climate).")

    def __init__(self, engine, db_path: Path | None = None, clock=time.monotonic) -> None:
        super().__init__("vehicle", engine)
        self._db_path = db_path
        self._db: sqlite3.Connection | None = None
        self._clock = clock
        self._last_proposal: float | None = None

    # ── history ───────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            path = self._db_path
            if path is None:
                from ..wallet import data_dir
                path = data_dir() / "vehicle.db"
            self._db = sqlite3.connect(str(path), check_same_thread=False)
            self._db.execute(_SCHEMA)
            self._db.commit()
        return self._db

    def log_action(self, action: str, args: dict | None = None, source: str = "user") -> None:
        import json
        db = self._conn()
        db.execute("INSERT INTO actions(ts, action, args, source) VALUES (?,?,?,?)",
                   (now_iso(), action, json.dumps(args or {}), source))
        db.commit()

    def history(self, limit: int = 10) -> list[dict]:
        import json
        rows = self._conn().execute(
            "SELECT ts, action, args, source FROM actions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": r[0], "action": r[1], "args": json.loads(r[2]), "source": r[3]} for r in rows]

    # ── proposals ─────────────────────────────────────────────
    def propose(self, weather_now: dict, forecast: dict, state: dict, prefs: dict | None = None) -> Proposal | None:
        """At most one proposal per ten minutes; the first matching rule wins."""
        prefs = prefs or {}
        if prefs.get("proactive") is False:
            return None
        t = self._clock()
        if self._last_proposal is not None and t - self._last_proposal < PROPOSAL_GAP_S:
            return None
        found = self._rules(weather_now or {}, forecast or {}, state or {}, prefs)
        if found is not None:
            self._last_proposal = t
        return found

    @staticmethod
    def _rules(now: dict, fc: dict, state: dict, prefs: dict) -> Proposal | None:
        out_now, out_next = now.get("temp_f"), fc.get("temp_f")
        if out_now is not None and out_next is not None and out_now - out_next >= DROP_F \
                and int(state.get("seat_heat", 0)) == 0:
            level = int(prefs.get("seat_heat_level", 2))
            return Proposal("seat_heat", "set_climate", {"seat_heat": level},
                            f"Outside is dropping from {out_now:.0f} to {out_next:.0f} °F; turn on the seat heater?")
        cabin_f = state.get("cabin_temp_f")
        if out_now is not None and cabin_f is not None and f_to_c(out_now) > HOT_OUTSIDE_C \
                and f_to_c(cabin_f) > HOT_CABIN_C and not state.get("ac_on", False):
            return Proposal("ac", "set_climate", {"ac_on": True, "temperature_f": round(AC_TARGET_C * 9 / 5 + 32)},
                            f"It is {f_to_c(out_now):.0f} °C outside and warm in the cabin; cool it to {AC_TARGET_C:.0f} °C?")
        rain = fc.get("rain_in_h")
        if rain is not None and 0 <= rain <= 1:
            return Proposal("windows", "suggest", {}, "Rain is expected within the hour; close the windows?",
                            simulated=True)
        return None

    # ── weather watch ─────────────────────────────────────────
    async def check_weather(self) -> Proposal | None:
        v = self.engine.vehicle
        wx = await self.engine.weather.forecast(v.lat, v.lon, 1)
        now, fc = weather_to_inputs(wx, unit_metric=wx.get("unit") == "°C")
        prop = self.propose(now, fc, v.snapshot(), {})
        if prop is not None and hasattr(self.engine, "emit"):
            self.engine.emit("proposal", prop.to_dict())
        return prop

    async def watch(self, interval_s: float = 300.0) -> None:
        while True:
            try:
                await self.check_weather()
            except asyncio.CancelledError:
                raise
            except Exception as e:                                   # noqa: BLE001
                log.info("weather watch: %s", type(e).__name__)
            await asyncio.sleep(interval_s)

    # ── agent entry ───────────────────────────────────────────
    async def execute(self, request: AgentRequest) -> tuple[dict, str]:
        ctx = request.context
        action = str(ctx.get("action") or self._intent(request.request))
        if action == "history":
            rows = self.history(int(ctx.get("limit") or 10))
            return {"actions": rows}, f"{len(rows)} recent climate actions"
        if action == "check":
            prop = await self.check_weather()
            return ({"proposal": prop.to_dict()} if prop else {"proposal": None}), \
                (prop.reason if prop else "Nothing to suggest right now")
        if action == "set":
            args = dict(ctx.get("args") or {})
            res, _c, _n = await self.engine.tools.run("set_climate", args)
            if res.get("error"):
                raise RuntimeError(res["error"])
            self.log_action("set_climate", args, str(ctx.get("source") or "user"))
        v = self.engine.vehicle.snapshot()
        state = {k: v[k] for k in ("cabin_temp_f", "seat_heat", "fan", "ac_on", "lat", "lon", "fix_state",
                                   "battery_pct", "range_mi") if k in v}
        return {"state": state}, (f"Cabin {state['cabin_temp_f']:.0f} °F, seat heat {state['seat_heat']}, "
                                  f"fan {state['fan']}, A/C {'on' if state['ac_on'] else 'off'}")

    @staticmethod
    def _intent(text: str) -> str:
        t = text.lower()
        if any(w in t for w in ("history", "earlier", "last time", "did i")):
            return "history"
        if any(w in t for w in ("suggest", "proposal", "should i", "weather")):
            return "check"
        return "status"
