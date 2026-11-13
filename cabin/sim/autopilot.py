"""Autopilot: a guided tour that drives the real engine through every feature.

Each step speaks a request as if the driver said it (listening UI, live
partial transcript, mic level, endpoint pause), then lets the engine think,
call tools, render cards and talk. Some steps press card buttons or talk
over the assistant to show interruption. Any real interaction (voice, tap,
typing) pauses the tour; P resumes, N skips ahead.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

log = logging.getLogger("autopilot")


@dataclass
class Step:
    say: str
    actions: tuple = ()         # card buttons to press afterwards: navigate | calendar | share | call
    barge: str = ""             # interrupt mid-reply with this request
    barge_after: float = 2.2
    hold: float = 4.0           # seconds to leave the result on screen


TOUR = [
    Step("Plan a weekend trip to Lake Tahoe, check battery, charging stops and weather.",
         actions=("calendar", "navigate"), hold=3.5),
    Step("Find a coffee shop nearby."),
    Step("What's the weather like in Napa this weekend?"),
    Step("Plan a road trip to Los Angeles on Saturday.", hold=4.5),
    Step("I'm a little cold. Set the cabin to 74 and warm my seat."),
    Step("What's the latest EV news?", barge="Actually, how much range do I have left?", barge_after=2.4),
    Step("Find sushi near Union Square.", actions=("share",), hold=3.0),
    Step("Take me to the Golden Gate Bridge.", hold=6.0),
    Step("Who won the Warriors game?"),
]


class Autopilot:
    def __init__(self, engine, steps: list[Step] | None = None) -> None:
        self.e = engine
        self.steps = steps or TOUR
        self.i = 0
        self.paused = False
        self.task: asyncio.Task | None = None

    # controls (call on the engine loop)
    def start(self, delay: float = 2.5) -> None:
        self.task = asyncio.ensure_future(self._run(delay))

    def pause(self, reason: str = "") -> None:
        if not self.paused:
            self.paused = True
            if self.task:
                self.task.cancel()
            self.e.emit("autopilot", {"running": False, "step": self.i})
            if reason:
                self.e.emit("toast", f"Tour paused — {reason}. Press P to resume")

    def toggle(self) -> None:
        if self.paused:
            self.paused = False
            self.e.emit("toast", "Tour resumed")
            self.start(0.8)
        else:
            self.pause()
            self.e.emit("toast", "Tour paused — press P to resume")

    def next(self) -> None:
        if self.task:
            self.task.cancel()
        self.i += 1
        self.paused = False

        async def go():
            await self.e._interrupt_to_idle()
            self.e.emit("nav", {})
            self.start(0.4)
        asyncio.ensure_future(go())

    # tour
    async def _wait_state(self, states: tuple[str, ...], timeout: float = 45) -> None:
        t = 0.0
        while self.e.state not in states and t < timeout:
            await asyncio.sleep(0.05)
            t += 0.05

    async def _run(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            self.e.emit("autopilot", {"running": True, "step": self.i})
            while not self.paused:
                st = self.steps[self.i % len(self.steps)]
                self.e.emit("autopilot", {"running": True, "step": self.i % len(self.steps) + 1,
                                          "total": len(self.steps)})
                await self.e.simulate_voice(st.say)
                if st.barge:
                    await self._wait_state(("speaking",))
                    await asyncio.sleep(st.barge_after)
                    if self.e.state == "speaking":
                        await self.e.simulate_voice(st.barge)
                await self._wait_state(("speaking",), 20)
                await self._wait_state(("idle",))
                for key in st.actions:
                    if not self.e.last_card or not any(a["key"] == key for a in self.e.last_card.get("actions", [])):
                        continue
                    await asyncio.sleep(1.4)
                    self.e.emit("press", key)          # UI presses the real button → same path as a tap
                    await asyncio.sleep(6.0 if key == "navigate" else 2.0)
                await asyncio.sleep(st.hold)
                if self.e.nav_open:
                    self.e.emit("nav", {})
                    self.e.nav_open = False
                    await asyncio.sleep(1.0)
                self.i += 1
        except asyncio.CancelledError:
            pass
