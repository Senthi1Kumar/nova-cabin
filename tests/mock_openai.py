"""Local OpenAI-compatible mock: streaming chat + tool calls, PCM TTS, transcription.

    python tests/mock_openai.py 8765
Point the app at it:  OPENAI_API_KEY=test OPENAI_BASE_URL=http://127.0.0.1:8765/v1 LLM_PROVIDER=openai
"""
import json
import math

import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

ROUTES = [("trip", "plan_trip", {"destination": "Lake Tahoe", "when": "Saturday 9am"}, "Let me map that out."),
          ("coffee", "find_places", {"query": "coffee"}, ""),
          ("weather", "get_weather", {"location": "Lake Tahoe"}, ""),
          ("news", "web_search", {"query": "latest tech news"}, "Checking the headlines."),
          ("cabin", "set_climate", {"temperature_f": 70, "seat_heat": 2}, ""),
          ("take me", "navigate_to", {"destination": "Golden Gate Bridge"}, "")]
FINAL = {"plan_trip": "Your Lake Tahoe trip is ready. It's about three and a half hours with one quick charge in "
                      "Reno, and you'll arrive with sixty-one percent.",
         "find_places": "The closest is Blue Bottle, about half a mile away. I've put a few others on the map.",
         "get_weather": "Tahoe will be crisp, highs around sixty and mostly sunny all weekend.",
         "web_search": "Top story right now is a new on-device AI chip announcement. Sources are on screen.",
         "set_climate": "Cabin's set to seventy and your seat heater is on level two.",
         "navigate_to": "Starting navigation to the Golden Gate Bridge. About twelve minutes."}


def sse(h, obj):
    h.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
    h.wfile.flush()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(n)
        if self.path.endswith("/chat/completions"):
            return self.chat(json.loads(raw))
        if self.path.endswith("/audio/speech"):
            return self.speech(json.loads(raw))
        if self.path.endswith("/audio/transcriptions"):
            return self.reply_json({"text": "Plan a weekend trip to Lake Tahoe"})
        self.send_response(404); self.end_headers()

    def reply_json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def chat(self, body):
        msgs = body["messages"]
        self.send_response(200)
        self.send_header("content-type", "text/event-stream"); self.end_headers()
        base = {"id": "x", "object": "chat.completion.chunk", "model": body["model"]}
        last = msgs[-1]
        time.sleep(0.35)                                   # first-token latency
        if last["role"] == "tool":
            name = next(tc["function"]["name"] for m in reversed(msgs) if m.get("tool_calls") for tc in m["tool_calls"])
            text = FINAL.get(name, "Done.")
            if '"error"' in last["content"]:
                text = "I couldn't reach that service just now. " + text
            for w in text.split(" "):
                sse(self, {**base, "choices": [{"index": 0, "delta": {"content": w + " "}}]}); time.sleep(0.03)
            sse(self, {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        else:
            q = last["content"].lower()
            hit = next((r for r in ROUTES if r[0] in q), None)
            if hit:
                key, name, args, pre = hit
                for w in pre.split():
                    sse(self, {**base, "choices": [{"index": 0, "delta": {"content": w + " "}}]}); time.sleep(0.03)
                a = json.dumps(args)
                sse(self, {**base, "choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "id": "call_1", "type": "function", "function": {"name": name, "arguments": ""}}]}}]})
                for i in range(0, len(a), 7):
                    sse(self, {**base, "choices": [{"index": 0, "delta": {"tool_calls": [
                        {"index": 0, "function": {"arguments": a[i:i + 7]}}]}}]})
                sse(self, {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
            else:
                for w in "Sure. I can help with trips, places, weather, news and the car.".split():
                    sse(self, {**base, "choices": [{"index": 0, "delta": {"content": w + " "}}]}); time.sleep(0.03)
                sse(self, {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        if (body.get("stream_options") or {}).get("include_usage"):
            sse(self, {**base, "choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 18, "cost": 0.0001}})
        self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()

    def speech(self, body):
        words = body["input"].split()
        dur = max(0.6, len(words) / 2.7)
        sr = 24000
        t = np.arange(int(sr * dur)) / sr
        env = np.abs(np.sin(t * 2 * math.pi * 2.3)) * (0.6 + 0.4 * np.sin(t * 0.9))
        f0 = 190 + 25 * np.sin(t * 3)
        x = 0.25 * env * np.sin(2 * math.pi * np.cumsum(f0) / sr)
        pcm = (x * 32767).astype(np.int16).tobytes()
        time.sleep(0.15)
        self.send_response(200); self.send_header("content-type", "audio/pcm")
        self.send_header("content-length", str(len(pcm))); self.end_headers(); self.wfile.write(pcm)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8765), H).serve_forever()
