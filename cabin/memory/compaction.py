"""Rolling compaction of an OpenAI-format conversation.

Older turns collapse into one summary message; the last KEEP_TURNS turns stay
verbatim. A turn starts at a user message, so an assistant tool_calls message
is never separated from its tool replies and the kept tail never starts with a
tool message.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger("memory.compaction")

KEEP_TURNS = 6
SUMMARY_PREFIX = "Summary of the earlier conversation: "
_TOOL_RESULT_CLIP = 400
_FALLBACK_CLIP = 1200


def estimate_tokens(messages: list[dict]) -> int:
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) // 4 + 1


def _is_summary(m: dict) -> bool:
    return m.get("role") == "system" and str(m.get("content") or "").startswith(SUMMARY_PREFIX)


def _split_turns(body: list[dict]) -> list[list[dict]]:
    turns: list[list[dict]] = []
    for m in body:
        if m.get("role") == "user" or not turns:
            turns.append([])
        turns[-1].append(m)
    return turns


def _render(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        role = m.get("role")
        if _is_summary(m):
            lines.append("earlier summary: " + str(m["content"])[len(SUMMARY_PREFIX):])
        elif role == "tool":
            lines.append("tool result: " + str(m.get("content") or "")[:_TOOL_RESULT_CLIP])
        elif role == "assistant" and m.get("tool_calls"):
            for c in m["tool_calls"]:
                fn = c.get("function", {})
                lines.append(f"tool call: {fn.get('name')}({str(fn.get('arguments') or '')[:200]})")
            if m.get("content"):
                lines.append("assistant: " + str(m["content"]))
        else:
            lines.append(f"{role}: {m.get('content') or ''}")
    return "\n".join(lines)


async def compact(messages: list[dict], budget_tokens: int,
                  summarise: Callable[[str], Awaitable[str]]) -> list[dict]:
    if estimate_tokens(messages) <= budget_tokens:
        return messages
    head = [m for m in messages if m.get("role") == "system" and not _is_summary(m)]
    rest = [m for m in messages if m not in head]
    turns = _split_turns(rest)
    if len(turns) <= KEEP_TURNS:
        return messages
    old = [m for t in turns[:-KEEP_TURNS] for m in t]
    kept = [m for t in turns[-KEEP_TURNS:] for m in t]
    text = _render(old)
    try:
        summary = (await summarise(text)).strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("summariser failed (%s); using a clipped digest", type(exc).__name__)
        summary = text[-_FALLBACK_CLIP:]
    return [*head, {"role": "system", "content": SUMMARY_PREFIX + summary}, *kept]
