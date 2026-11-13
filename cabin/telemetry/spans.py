"""Per-turn latency spans and a generic timed span."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

ORDER = ("speech_end", "stt_done", "first_token", "tools_done", "tts_first_chunk", "first_audio", "done")
_STAGE_FIELD = {
    "stt_done": "stt_ms", "first_token": "first_token_ms", "tools_done": "tools_ms",
    "tts_first_chunk": "tts_first_ms", "first_audio": "first_audio_ms",
}


class TurnSpans:
    """Marks the milestones of one turn; the stage durations add up to ``total_ms``.

    Stages are measured between consecutive milestones in canonical order. A milestone that never
    happened reports ``None`` and its time folds into the next one that did; a milestone stamped out
    of order is clamped to the previous one so the sum stays exact.
    """

    def __init__(self, turn_id: str, clock: Callable[[], float] = time.monotonic,
                 on_done: Callable[[dict], None] | None = None) -> None:
        self.turn_id = turn_id
        self._clock = clock
        self._on_done = on_done
        self._marks: dict[str, float] = {}
        self._finished = False
        self.wall_start = time.time()

    def mark(self, name: str, at: float | None = None, replace: bool = False) -> None:
        """Stamp a milestone. ``at`` is an explicit clock time (``record`` clamps it so stages stay
        ordered); ``replace`` moves an already-stamped milestone to the new time (not ``done``)."""
        if name not in ORDER or (name in self._marks and not (replace and name != "done")):
            return
        self._marks[name] = self._clock() if at is None else at
        if name == "done" and not self._finished:
            self._finished = True
            if self._on_done is not None:
                self._on_done(self.record())

    def record(self) -> dict[str, Any]:
        present = [n for n in ORDER if n in self._marks]
        rec: dict[str, Any] = {"kind": "turn", "turn_id": self.turn_id, "ts": self.wall_start,
                               "speech_end": self._marks.get("speech_end"),
                               **{f: None for f in _STAGE_FIELD.values()}}
        if not present:
            return {**rec, "tail_ms": None, "total_ms": None, "ttfa_ms": None}
        origin = prev = self._marks[present[0]]
        clamped: dict[str, float] = {present[0]: origin}
        for n in present[1:]:
            t = max(self._marks[n], prev)
            clamped[n] = t
            field = _STAGE_FIELD.get(n)
            if field:
                rec[field] = (t - prev) * 1000.0
            elif n == "done":
                rec["tail_ms"] = (t - prev) * 1000.0
            prev = t
        # a first milestone that is itself a stage end (no speech_end) has no duration of its own
        rec.setdefault("tail_ms", None)
        rec["total_ms"] = (prev - origin) * 1000.0
        if "first_audio" in clamped:
            rec["ttfa_ms"] = (clamped["first_audio"] - origin) * 1000.0
        else:
            rec["ttfa_ms"] = None
        if rec["tail_ms"] is None and "done" not in clamped:
            rec["tail_ms"] = (prev - origin) * 1000.0 - sum(
                rec[f] or 0.0 for f in _STAGE_FIELD.values())
        return rec


class Span:
    """A named timed section: ``Span("map").start()`` ... ``.end(frames=3)``."""

    def __init__(self, name: str, clock: Callable[[], float] = time.monotonic,
                 emit: Callable[[dict], None] | None = None) -> None:
        self.name = name
        self._clock = clock
        self._emit = emit
        self._t0: float | None = None

    def start(self) -> Span:
        self._t0 = self._clock()
        return self

    def end(self, **fields: Any) -> dict[str, Any]:
        t0 = self._t0 if self._t0 is not None else self._clock()
        rec = {"kind": "span", "name": self.name, "ms": (self._clock() - t0) * 1000.0, **fields}
        if self._emit is not None:
            self._emit(rec)
        return rec
