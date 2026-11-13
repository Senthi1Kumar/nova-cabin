"""Plugin entry: builds the agent registry and registers the built-in agents."""
from __future__ import annotations

import datetime as dt

from .base import Agent, AgentRequest
from .nav import NavigationAgent
from .registry import AgentRegistry
from .research import ResearchAgent
from .sales import SalesAgent
from .vehicle import VehicleAgent


class TimeAgent(Agent):
    description = "Current local date, time and timezone of the vehicle."

    def __init__(self, engine) -> None:
        super().__init__("time", engine)

    async def execute(self, request: AgentRequest) -> tuple[dict, str]:
        now = dt.datetime.now().astimezone()
        data = {"iso": now.isoformat(timespec="seconds"), "date": now.strftime("%A, %d %B %Y"),
                "time": now.strftime("%H:%M"), "timezone": now.tzname() or "", "utc_offset": now.strftime("%z")}
        return data, f"{data['date']}, {data['time']} {data['timezone']}"


def install(engine) -> None:
    registry = AgentRegistry(engine)
    engine.agents = registry
    registry.register(TimeAgent(engine))
    registry.register(NavigationAgent(engine))
    registry.register(SalesAgent(engine))
    registry.register(VehicleAgent(engine))
    registry.register(ResearchAgent(engine))
