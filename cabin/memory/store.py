"""Session store: every turn of a conversation, persisted as provider-neutral rows.

One row is one assistant step (text, the tool calls it made, their results, the
cards it showed) or one user message. Loading rebuilds an OpenAI-format message
list with tool-call ids renumbered, so the same conversation continues on any
provider, and the list never starts on an orphan tool message.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .compaction import estimate_tokens

DEFAULT_BUDGET_TOKENS = 3500


@dataclass
class Session:
    id: int
    driver_id: str
    started_at: float
    updated_at: float


class SessionStore:
    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            from .profile import default_db_path
            path = default_db_path()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._tx() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS sessions ("
                       "id INTEGER PRIMARY KEY AUTOINCREMENT, driver_id TEXT NOT NULL, "
                       "started_at REAL NOT NULL, updated_at REAL NOT NULL, "
                       "summary TEXT NOT NULL DEFAULT '', summary_upto INTEGER NOT NULL DEFAULT 0)")
            db.execute("CREATE TABLE IF NOT EXISTS turns ("
                       "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL, "
                       "turn_no INTEGER NOT NULL, role TEXT NOT NULL, text TEXT, "
                       "tool_calls TEXT NOT NULL DEFAULT '[]', tool_results TEXT NOT NULL DEFAULT '[]', "
                       "cards TEXT NOT NULL DEFAULT '[]', ts REAL NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS turns_session ON turns(session_id, id)")

    @contextlib.contextmanager
    def _tx(self):
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA synchronous=NORMAL")
            yield db

    # ── sessions ─────────────────────────────────────────────────
    def open_session(self, driver_id: str) -> Session:
        now = time.time()
        with self._tx() as db:
            cur = db.execute("INSERT INTO sessions (driver_id, started_at, updated_at) VALUES (?,?,?)",
                             (driver_id, now, now))
            return Session(cur.lastrowid, driver_id, now, now)

    def last_session(self, driver_id: str) -> Session | None:
        with self._tx() as db:
            row = db.execute("SELECT id, driver_id, started_at, updated_at FROM sessions "
                             "WHERE driver_id=? ORDER BY id DESC LIMIT 1", (driver_id,)).fetchone()
        return Session(*row) if row else None

    # ── turns ────────────────────────────────────────────────────
    def record_turn(self, session_id: int, role: str, text: str | None, tool_calls: list | None = None,
                    tool_results: list | None = None, cards: list | None = None) -> int:
        now = time.time()
        with self._tx() as db:
            n = db.execute("SELECT COALESCE(MAX(turn_no), 0) FROM turns WHERE session_id=?",
                           (session_id,)).fetchone()[0]
            if role == "user":
                n += 1
            cur = db.execute(
                "INSERT INTO turns (session_id, turn_no, role, text, tool_calls, tool_results, cards, ts) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (session_id, n, role, text, json.dumps(tool_calls or [], ensure_ascii=False),
                 json.dumps(tool_results or [], ensure_ascii=False),
                 json.dumps(cards or [], ensure_ascii=False), now))
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
            return cur.lastrowid

    def record_rows(self, session_id: int, rows: list[dict]) -> None:
        """Insert several rows of one turn in a single transaction."""
        if not rows:
            return
        now = time.time()
        with self._tx() as db:
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                return                                   # the session was purged; drop the stale write
            n = db.execute("SELECT COALESCE(MAX(turn_no), 0) FROM turns WHERE session_id=?",
                           (session_id,)).fetchone()[0]
            for r in rows:
                if r["role"] == "user":
                    n += 1
                db.execute(
                    "INSERT INTO turns (session_id, turn_no, role, text, tool_calls, tool_results, cards, ts) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (session_id, n, r["role"], r.get("text"),
                     json.dumps(r.get("tool_calls") or [], ensure_ascii=False),
                     json.dumps(r.get("tool_results") or [], ensure_ascii=False),
                     json.dumps(r.get("cards") or [], ensure_ascii=False), now))
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))

    def purge(self, driver_id: str) -> int:
        """Delete every stored turn and summary for the driver; returns the highest turn id seen afterwards."""
        with self._tx() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM sessions WHERE driver_id=?", (driver_id,))]
            for sid in ids:
                db.execute("DELETE FROM turns WHERE session_id=?", (sid,))
            db.execute("DELETE FROM sessions WHERE driver_id=?", (driver_id,))
            return db.execute("SELECT COALESCE(MAX(id),0) FROM turns").fetchone()[0]

    def max_turn_id(self, session_id: int | None = None) -> int:
        with self._tx() as db:
            if session_id is None:
                return db.execute("SELECT COALESCE(MAX(id),0) FROM turns").fetchone()[0]
            return db.execute("SELECT COALESCE(MAX(id),0) FROM turns WHERE session_id=?",
                              (session_id,)).fetchone()[0]

    def session_count(self) -> int:
        with self._tx() as db:
            return db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def user_turn_count(self, session_id: int) -> int:
        with self._tx() as db:
            return db.execute("SELECT COUNT(*) FROM turns WHERE session_id=? AND role='user'",
                              (session_id,)).fetchone()[0]

    def set_summary(self, session_id: int, summary: str, upto_turn: int) -> None:
        with self._tx() as db:
            db.execute("UPDATE sessions SET summary=?, summary_upto=? WHERE id=?",
                       (summary, max(0, upto_turn), session_id))

    def summary(self, session_id: int) -> str:
        with self._tx() as db:
            row = db.execute("SELECT summary FROM sessions WHERE id=?", (session_id,)).fetchone()
        return row[0] if row else ""

    def texts_since(self, session_id: int, after_id: int = 0) -> list[dict]:
        """User and assistant text rows after `after_id`, for profile mining."""
        with self._tx() as db:
            rows = db.execute("SELECT id, role, text, ts FROM turns WHERE session_id=? AND id>? "
                              "AND role IN ('user','assistant') AND text IS NOT NULL AND text<>'' ORDER BY id",
                              (session_id, after_id)).fetchall()
        return [{"turn_id": i, "role": r, "content": t, "ts": ts} for i, r, t, ts in rows]

    def load_messages(self, session_id: int, budget_tokens: int = DEFAULT_BUDGET_TOKENS) -> list[dict]:
        with self._tx() as db:
            upto = db.execute("SELECT summary_upto FROM sessions WHERE id=?", (session_id,)).fetchone()
            rows = db.execute("SELECT turn_no, role, text, tool_calls, tool_results FROM turns "
                              "WHERE session_id=? AND turn_no>? ORDER BY id",
                              (session_id, upto[0] if upto else 0)).fetchall()
        messages: list[dict] = []
        counter = 0
        for _no, role, text, calls_json, results_json in rows:
            calls, results = json.loads(calls_json), {r.get("id"): r for r in json.loads(results_json)}
            if role != "assistant" or not calls:
                if role in ("user", "assistant"):
                    messages.append({"role": role, "content": text or ""})
                continue
            if any(c.get("id") not in results for c in calls):          # interrupted before every result landed
                if text:
                    messages.append({"role": "assistant", "content": text})
                continue
            renamed = []
            for c in calls:
                counter += 1
                renamed.append((f"call_{counter}", c))
            messages.append({"role": "assistant", "content": text or None, "tool_calls": [
                {"id": new, "type": "function",
                 "function": {"name": c.get("name", ""), "arguments": c.get("arguments") or "{}"}}
                for new, c in renamed]})
            for new, c in renamed:
                messages.append({"role": "tool", "tool_call_id": new,
                                 "content": results[c["id"]].get("content", "")})
        return _fit(messages, budget_tokens)


def _fit(messages: list[dict], budget_tokens: int) -> list[dict]:
    """Drop the oldest whole turns until the list fits; always start on a user message."""
    while messages and messages[0].get("role") != "user":
        messages.pop(0)
    while messages and estimate_tokens(messages) > budget_tokens:
        nxt = next((i for i, m in enumerate(messages) if i > 0 and m.get("role") == "user"), None)
        if nxt is None:
            break
        del messages[:nxt]
    return messages
