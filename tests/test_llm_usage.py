"""ChatClient usage reporting: final usage event, and a provider that rejects stream_options still answers."""
import asyncio
import json

import httpx

from cabin.llm import ChatClient

SSE = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
       b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
       b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2,"cost":0.001}}\n\n'
       b"data: [DONE]\n\n")


def client(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ChatClient(http, {"provider": "openrouter", "label": "X", "base_url": "http://x", "api_key": "k",
                             "model": "m"})


async def collect(c):
    return [ev async for ev in c.stream([{"role": "user", "content": "hi"}])]


def test_usage_event_is_yielded():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, content=SSE)
    evs = asyncio.run(collect(client(handler)))
    assert seen[0]["stream_options"] == {"include_usage": True}
    assert ("usage", {"prompt_tokens": 7, "completion_tokens": 2, "cost": 0.001}) in evs
    assert evs[-1][0] == "done"


def test_400_on_stream_options_retries_without_and_remembers():
    bodies = []

    def handler(req):
        b = json.loads(req.content)
        bodies.append(b)
        if "stream_options" in b:
            return httpx.Response(400, json={"error": {"message": "unknown field stream_options"}})
        return httpx.Response(200, content=SSE)
    c = client(handler)
    evs = asyncio.run(collect(c))
    assert [("stream_options" in b) for b in bodies] == [True, False]
    assert ("text", "hi") in evs and evs[-1][0] == "done"
    asyncio.run(collect(c))
    assert len(bodies) == 3 and "stream_options" not in bodies[-1]


def test_other_400_still_raises():
    import pytest

    from cabin.llm import LLMError

    def handler(req):
        return httpx.Response(400, json={"error": "bad model"})
    with pytest.raises(LLMError):
        asyncio.run(collect(client(handler)))
