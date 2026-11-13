"""Streaming chat client for every OpenAI-compatible endpoint.

OpenAI, OpenRouter, Gemini (OpenAI compatibility layer), xAI Grok,
Anthropic (OpenAI SDK compatibility) and Groq all speak the same
/chat/completions + tools protocol, so one implementation covers them.

    async for ev in llm.stream(messages, tools):
        ev = ("text", "chunk") | ("tool_calls", [{id, name, arguments}]) | ("usage", {prompt_tokens, completion_tokens, cost?}) | ("done", finish_reason)
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx

log = logging.getLogger("llm")


class LLMError(RuntimeError):
    pass


class ChatClient:
    def __init__(self, http: httpx.AsyncClient, conf: dict) -> None:
        self.http = http
        self.provider = conf["provider"]
        self.label = conf["label"]
        self.base = conf["base_url"]
        self.key = conf["api_key"]
        self.model = conf["model"]
        self.temperature = conf.get("temperature") or ""
        self.reasoning = conf.get("reasoning_effort") or ""
        self.usage_ok = self.provider != "anthropic"

    def _headers(self) -> dict:
        h = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        if self.provider == "openrouter":
            h["HTTP-Referer"] = "https://elevatics.ai/cabin-assistant"
            h["X-Title"] = "Cabin Assistant"
        return h

    async def stream(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[tuple]:
        body: dict = {"model": self.model, "messages": messages, "stream": True}
        if self.usage_ok:
            body["stream_options"] = {"include_usage": True}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if self.temperature:
            body["temperature"] = float(self.temperature)
        if self.reasoning:
            body["reasoning_effort"] = self.reasoning

        calls: dict[int, dict] = {}
        finish = None
        usage = None
        for attempt in (0, 1):
            async with self.http.stream("POST", f"{self.base}/chat/completions", json=body,
                                        headers=self._headers(), timeout=httpx.Timeout(60, connect=10)) as r:
                if r.status_code >= 400:
                    detail = (await r.aread()).decode(errors="ignore")[:400]
                    if (attempt == 0 and r.status_code == 400 and "stream_options" in body
                            and ("stream_options" in detail or "include_usage" in detail)):
                        self.usage_ok = False
                        body.pop("stream_options")
                        continue
                    raise LLMError(f"{self.label} {r.status_code}: {detail}")
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if "error" in chunk:
                        raise LLMError(f"{self.label}: {chunk['error']}")
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for ch in chunk.get("choices", []):
                        delta = ch.get("delta") or {}
                        if delta.get("content"):
                            yield ("text", delta["content"])
                        for tc in delta.get("tool_calls") or []:
                            i = tc.get("index")
                            if i is None:                 # some providers omit index (one call per chunk)
                                known = [k for k, c in calls.items() if tc.get("id") and c["id"] == tc["id"]]
                                i = known[0] if known else (len(calls) if tc.get("id") or not calls else max(calls))
                            slot = calls.setdefault(i, {"id": "", "name": "", "arguments": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
                        if ch.get("finish_reason"):
                            finish = ch["finish_reason"]
            break
        if calls:
            out = []
            for i in sorted(calls):
                c = calls[i]
                if not c["id"]:
                    c["id"] = f"call_{i}"
                out.append(c)
            yield ("tool_calls", out)
        if usage:
            yield ("usage", usage)
        yield ("done", finish or ("tool_calls" if calls else "stop"))
