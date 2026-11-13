"""Specialist agents the brain calls as tools; each returns a typed ``AgentResult``."""
from .base import Agent, AgentRequest, AgentResult
from .registry import AgentRegistry

__all__ = ["Agent", "AgentRegistry", "AgentRequest", "AgentResult"]
