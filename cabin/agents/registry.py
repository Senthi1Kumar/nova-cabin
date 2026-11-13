"""Registry exposing each agent to the brain as an ``agent_<name>`` tool."""
from __future__ import annotations

from .base import Agent, AgentRequest


class AgentRegistry:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.agents: dict[str, Agent] = {}

    def get(self, name: str) -> Agent | None:
        return self.agents.get(name)

    def register(self, agent: Agent) -> None:
        self.agents[agent.name] = agent
        tool = f"agent_{agent.name}"
        schema = {"type": "function", "function": {
            "name": tool,
            "description": agent.description,
            "parameters": {"type": "object", "properties": {
                "request": {"type": "string", "description": "What to ask this specialist, in plain words"},
                "context": {"type": "object", "description": "Optional structured hints for the specialist"}},
                "required": ["request"]}}}

        async def method(request: str = "", context: dict | None = None):
            res = await agent.run(AgentRequest(request, context if isinstance(context, dict) else {}))
            return res.to_tool_result(), None, None

        self.engine.tools.extend([schema], {tool: method}, {tool: f"Asking {agent.name} agent"})
