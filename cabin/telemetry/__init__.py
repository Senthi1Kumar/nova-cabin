"""Telemetry: logs, conversation, tool I/O and per-turn latency spans, redacted at the sink.

    telemetry.install(cfg)          # singleton; flag TELEMETRY=off keeps every call a no-op
    telemetry.install_logging(cfg)  # also attaches the app.jsonl log handler and tees stdout/stderr
    tel.event(kind, value)          # engine emit tap
    tel.turn().mark("speech_end")   # TurnSpans
    tel.tool(...), tel.llm(...)     # tool and model call records
"""
from __future__ import annotations

import atexit
import itertools
import logging
import os
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .redact import add_secret, redact
from .sink import Sink
from .spans import Span, TurnSpans

__all__ = ["Span", "Telemetry", "TurnSpans", "get", "install", "install_logging", "redact", "reset"]

_CONV_TEXT = {"user": "user", "reply": "assistant"}
_TOOL_EVENTS = ("card", "nav", "checkout")
_SECRET_SETTING = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


def _opt(cfg, key: str, default: str = "") -> str:
    val = cfg.get(key) if cfg is not None else None
    return str(val or os.environ.get(key) or default)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text") or value.get("sentence") or "")
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return "" if value is None else str(value)


def _summarise(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in list(value.items())[:24]:
            if isinstance(v, (list, tuple, set)):
                out[k] = len(v)
            elif isinstance(v, dict):
                out[k] = {kk: vv for kk, vv in list(v.items())[:8] if isinstance(vv, (str, int, float, bool))}
            elif isinstance(v, str):
                out[k] = v[:160]
            elif isinstance(v, (int, float, bool)) or v is None:
                out[k] = v
            else:
                out[k] = type(v).__name__
        return out
    return _text(value)[:160]


class Telemetry:
    def __init__(self, sink: Sink | None, root: Path | None = None) -> None:
        self.sink = sink
        self.root = root
        self._counters: Counter[str] = Counter()
        self._turn: TurnSpans | None = None
        self._acc: dict[str, dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self._prefix = time.strftime("%H%M%S")
        self._cleanup: list = []
        self._closed = False

    @property
    def enabled(self) -> bool:
        return self.sink is not None

    def _put(self, stream: str, rec: dict, context: str = "") -> None:
        if self.sink is not None:
            self.sink.write(stream, rec, context)

    # ── engine tap ──
    def event(self, kind: str, value: Any = None) -> None:
        self._counters[kind] += 1
        if self.sink is None:
            return
        now = time.time()
        tid = self._turn.turn_id if self._turn else None
        if kind == "user":
            rec = {"kind": "text", "ts": now, "turn_id": tid, "role": "user", "text": _text(value),
                   "partial": bool(value.get("partial")) if isinstance(value, dict) else False}
            if isinstance(value, dict) and value.get("confidence") is not None:
                rec["confidence"] = value["confidence"]
            self._put("conversation", rec)
        elif kind == "reply":
            self._put("conversation", {"kind": "text", "ts": now, "turn_id": tid, "role": "assistant",
                                       "text": _text(value)})
        elif kind == "words":
            self._put("conversation", {"kind": "tts", "ts": now, "turn_id": tid, "text": _text(value)})
        elif kind in _TOOL_EVENTS:
            self._put("tools", {"kind": "event", "event": kind, "ts": now, "turn_id": tid,
                                "summary": _summarise(value)}, context=kind)

    def counters(self) -> dict[str, int]:
        return dict(self._counters)

    # ── turns, tools, models ──
    def turn(self) -> TurnSpans:
        tid = f"{self._prefix}-{next(self._ids)}"
        ts = TurnSpans(tid, on_done=self._turn_done)
        self._turn = ts
        self._acc[tid] = {"model": None, "tokens_in": 0, "tokens_out": 0, "cost": 0.0}
        for old in list(self._acc)[:-8]:
            self._acc.pop(old, None)
        return ts

    def _turn_done(self, rec: dict) -> None:
        rec.update(self._acc.pop(rec["turn_id"], {}))
        self._put("perf", rec)

    def tool(self, name: str, args: Any, result: Any, duration_s: float, agent: str | None = None,
             error: str | None = None) -> None:
        self._counters["tool"] += 1
        self._put("tools", {"kind": "tool", "ts": time.time(), "turn_id": self._turn.turn_id if self._turn else None,
                            "agent": agent, "name": name, "args": args, "result": result,
                            "duration_ms": duration_s * 1000.0, "error": error}, context=name)

    def llm(self, model: str, provider: str, tokens_in: int, tokens_out: int, cost: float,
            first_token_s: float | None, total_s: float | None) -> None:
        self._counters["llm"] += 1
        tid = self._turn.turn_id if self._turn else None
        acc = self._acc.get(tid or "")
        if acc is not None:
            acc["model"] = model
            acc["tokens_in"] += tokens_in or 0
            acc["tokens_out"] += tokens_out or 0
            acc["cost"] += cost or 0.0
        self._put("conversation", {"kind": "llm", "ts": time.time(), "turn_id": tid, "model": model,
                                   "provider": provider, "tokens_in": tokens_in, "tokens_out": tokens_out,
                                   "cost": cost, "first_token_s": first_token_s, "total_s": total_s})

    def perf(self, **fields: Any) -> None:
        self._put("perf", {"kind": "sample", "ts": time.time(), **fields})

    def span(self, name: str) -> Span:
        return Span(name, emit=lambda rec: self._put("perf", {"ts": time.time(), **rec}))

    # ── lifecycle ──
    def flush(self) -> None:
        if self.sink is not None:
            self.sink.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fn in self._cleanup:
            fn()
        self._cleanup.clear()
        if self.sink is not None:
            self.sink.close()

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "dir": str(self.root) if self.root else None,
                "dropped": self.sink.dropped if self.sink else 0, "errors": self.sink.errors if self.sink else 0}


_instance: Telemetry | None = None
_lock = threading.Lock()


def get() -> Telemetry | None:
    return _instance


def install(cfg=None) -> Telemetry:
    global _instance
    with _lock:
        if _instance is not None:
            return _instance
        if _opt(cfg, "TELEMETRY", "on").lower() in ("off", "0", "false", "no"):
            _instance = Telemetry(None)
            return _instance
        base = Path(_opt(cfg, "NOVA_CABIN_DATA") or Path.home() / ".local/share/nova-cabin")
        root = base / "logs"
        try:
            mb = float(_opt(cfg, "TELEMETRY_MAX_MB", "500"))
            days = float(_opt(cfg, "TELEMETRY_MAX_DAYS", "14"))
            qn = int(float(_opt(cfg, "TELEMETRY_QUEUE", "10000")))
            sink = Sink(root, max_bytes=int(mb * 1024 * 1024), max_days=days, queue_size=qn,
                        sqlite=_opt(cfg, "TELEMETRY_SQLITE", "on").lower() not in ("off", "0", "false", "no"))
        except Exception:  # noqa: BLE001 - unwritable log dir degrades to no-op
            _instance = Telemetry(None)
            return _instance
        raw = getattr(cfg, "raw", None) or {}
        for k, v in list(raw.items()) + list(os.environ.items()):
            if k.endswith(_SECRET_SETTING):
                add_secret(v)
        _instance = Telemetry(sink, root)
        atexit.register(_instance.close)
        return _instance


class _JsonlHandler(logging.Handler):
    def __init__(self, tel: Telemetry) -> None:
        super().__init__()
        self.tel = tel
        self._busy = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._busy, "on", False):
            return
        self._busy.on = True
        try:
            msg = record.getMessage()
            rec = {"kind": "log", "ts": record.created, "level": record.levelname, "logger": record.name,
                   "msg": msg}
            if record.exc_info and record.exc_info[0] is not None:
                rec["exc"] = self.format(record).split("\n", 1)[-1][-2000:]
            self.tel._put("app", rec)
        except Exception:  # noqa: BLE001, S110 - logging must never raise
            pass
        finally:
            self._busy.on = False


class _Tee:
    def __init__(self, real, name: str, tel: Telemetry) -> None:
        self._real, self._name, self._tel = real, name, tel
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, s):
        n = self._real.write(s)
        with self._lock:
            self._buf += s if isinstance(s, str) else s.decode("utf-8", "replace")
            *lines, self._buf = self._buf.split("\n")
            self._buf = self._buf[-4000:]
        for ln in lines:
            if ln.strip():
                self._tel._put("app", {"kind": self._name, "ts": time.time(), "msg": ln})
        return n

    def flush_partial(self) -> None:
        with self._lock:
            tail, self._buf = self._buf, ""
        if tail.strip():
            self._tel._put("app", {"kind": self._name, "ts": time.time(), "msg": tail})

    def __getattr__(self, item):
        return getattr(self._real, item)


def install_logging(cfg=None) -> Telemetry:
    tel = install(cfg)
    if tel.sink is None or tel._cleanup:
        return tel
    handler = _JsonlHandler(tel)
    root = logging.getLogger()
    root.addHandler(handler)
    out, err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _Tee(out, "stdout", tel), _Tee(err, "stderr", tel)

    def undo() -> None:
        for st in (sys.stdout, sys.stderr):
            if isinstance(st, _Tee):
                st.flush_partial()
        root.removeHandler(handler)
        if isinstance(sys.stdout, _Tee):
            sys.stdout = out
        if isinstance(sys.stderr, _Tee):
            sys.stderr = err

    tel._cleanup.append(undo)
    return tel


def reset() -> None:
    global _instance
    with _lock:
        if _instance is not None:
            _instance.close()
        _instance = None
