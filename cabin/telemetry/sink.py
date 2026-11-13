"""Background JSONL + SQLite sink: redacts, never blocks the caller, rotates by size and age."""
from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .redact import redact, scrub_text

STREAMS = ("app", "conversation", "tools", "perf")

_SCHEMA = (
    ("create table if not exists turns (id integer primary key, ts real, turn_id text, speech_end real, "
     "stt_ms real, first_token_ms real, tools_ms real, tts_first_ms real, first_audio_ms real, tail_ms real, "
     "total_ms real, ttfa_ms real, model text, tokens_in integer, tokens_out integer, cost real, json text)"),
    ("create table if not exists tools (id integer primary key, ts real, turn_id text, agent text, "
     "name text, duration_ms real, error text, json text)"),
    ("create table if not exists llm (id integer primary key, ts real, turn_id text, model text, "
     "provider text, tokens_in integer, tokens_out integer, cost real, first_token_s real, total_s real, "
     "json text)"),
)
_COLS = {
    "turns": ("ts", "turn_id", "speech_end", "stt_ms", "first_token_ms", "tools_ms", "tts_first_ms",
              "first_audio_ms", "tail_ms", "total_ms", "ttfa_ms", "model", "tokens_in", "tokens_out", "cost"),
    "tools": ("ts", "turn_id", "agent", "name", "duration_ms", "error"),
    "llm": ("ts", "turn_id", "model", "provider", "tokens_in", "tokens_out", "cost", "first_token_s", "total_s"),
}


def _table_for(stream: str, rec: dict) -> str | None:
    kind = rec.get("kind")
    if stream == "perf" and kind == "turn":
        return "turns"
    if stream == "tools" and kind == "tool":
        return "tools"
    if stream == "conversation" and kind == "llm":
        return "llm"
    return None


class _File:
    def __init__(self, path: Path, now: float) -> None:
        self.path = path
        self.fh = open(path, "ab", buffering=0)  # noqa: SIM115 - held open for the process lifetime
        self.size = path.stat().st_size
        self.opened = now


class Sink:
    def __init__(self, root: Path | str, max_bytes: int = 500 * 1024 * 1024, max_days: float = 14,
                 queue_size: int = 10000, clock: Callable[[], float] = time.time,
                 sqlite: bool = True, _hold: threading.Event | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = int(max_bytes)
        self.max_days = max_days
        self.dropped = 0
        self.errors = 0
        self._clock = clock
        self._use_sqlite = sqlite
        self._hold = _hold
        self._q: queue.Queue = queue.Queue(maxsize=queue_size)
        self._files: dict[str, _File] = {}
        self._db: sqlite3.Connection | None = None
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="telemetry-sink", daemon=True)
        self._thread.start()

    # ── caller side ──
    def write(self, stream: str, rec: dict[str, Any], context: str = "") -> None:
        if self._closed:
            return
        try:
            clean = redact(rec, context)
            line = json.dumps(clean, default=lambda o: scrub_text(str(o)), ensure_ascii=False)
        except Exception:  # noqa: BLE001 - a bad record must not reach the caller
            self.errors += 1
            return
        try:
            self._q.put_nowait((stream, line, clean))
        except queue.Full:
            self.dropped += 1

    def flush(self, timeout: float = 2.0) -> None:
        end = time.monotonic() + timeout
        while self._q.unfinished_tasks and time.monotonic() < end:
            time.sleep(0.005)

    def close(self) -> None:
        if self._closed:
            return
        self.flush(1.0)
        self._closed = True
        try:
            self._q.put(None, timeout=0.5)
        except queue.Full:
            return
        self._thread.join(timeout=1)

    # ── writer thread ──
    def _run(self) -> None:
        if self._hold is not None:
            self._hold.wait()
        while True:
            item = self._q.get()
            try:
                if item is None:
                    break
                self._write(*item)
            except Exception:  # noqa: BLE001 - keep the writer alive
                self.errors += 1
            finally:
                self._q.task_done()
        for f in self._files.values():
            f.fh.close()
        if self._db is not None:
            self._db.close()

    def _open(self, stream: str) -> _File:
        path = self.root / f"{stream}.jsonl"
        now = self._clock()
        if path.exists():
            try:
                if now - path.stat().st_mtime > self.max_days * 86400:
                    self._archive(path, now)
            except OSError:
                pass
        f = _File(path, now)
        self._files[stream] = f
        self._prune(stream, now)
        return f

    def _archive(self, path: Path, now: float) -> None:
        stem = path.name[: -len(".jsonl")]
        n = 0
        while True:
            dest = path.with_name(f"{stem}.{int(now)}{'' if n == 0 else f'-{n}'}.jsonl")
            if not dest.exists():
                break
            n += 1
        os.replace(path, dest)

    def _prune(self, stream: str, now: float) -> None:
        cutoff = now - self.max_days * 86400
        for p in self.root.glob(f"{stream}.*.jsonl"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass

    def _write(self, stream: str, line: str, rec: dict) -> None:
        data = (line + "\n").encode()
        f = self._files.get(stream) or self._open(stream)
        now = self._clock()
        if f.size > 0 and (f.size + len(data) > self.max_bytes or now - f.opened >= self.max_days * 86400):
            f.fh.close()
            self._archive(f.path, now)
            f = self._files[stream] = _File(f.path, now)
            self._prune(stream, now)
        f.fh.write(data)
        f.size += len(data)
        table = _table_for(stream, rec) if self._use_sqlite else None
        if table:
            self._mirror(table, rec, line)

    def _mirror(self, table: str, rec: dict, line: str) -> None:
        if self._db is None:
            self._db = sqlite3.connect(self.root / "telemetry.db")
            self._db.execute("pragma journal_mode=wal")
            for stmt in _SCHEMA:
                self._db.execute(stmt)
        cols = _COLS[table]
        vals = [rec.get(c) if not isinstance(rec.get(c), (dict, list)) else json.dumps(rec.get(c))
                for c in cols]
        self._db.execute(
            f"insert into {table} ({','.join(cols)},json) values ({','.join('?' * (len(cols) + 1))})",
            [*vals, line])
        self._db.commit()
