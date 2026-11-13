"""Conversation memory: persisted sessions, rolling compaction and the driver profile."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

from .compaction import SUMMARY_PREFIX, compact, estimate_tokens
from .profile import ProfileStore, extract_facts, handle_memory_intent, profile_line
from .store import DEFAULT_BUDGET_TOKENS, Session, SessionStore

log = logging.getLogger("memory")

MINE_EVERY_TURNS = 10
RESUME_WINDOW_S = 4 * 3600
SUMMARY_TIMEOUT_S = 6.0
_SUMMARY_SYSTEM = ("Summarise this earlier part of a driver's conversation with an in-car assistant in under "
                   "120 words. Keep facts the assistant may need again: places, orders, routes, choices, "
                   "tool outcomes. Plain text only.")

_PURGING = ("forget_all", "forget_field", "forget_value")
FLUSH_TIMEOUT_S = 3.0

__all__ = ["Memory", "ProfileStore", "Session", "SessionStore", "compact", "estimate_tokens", "memory_status"]


def memory_status(path: str | Path) -> dict:
    """Counts only, never contents: session count and the time of the last profile update."""
    path = Path(path)
    if not path.exists():
        return {"sessions": 0, "last_mined": None}
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    try:
        sessions = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        try:
            last = db.execute("SELECT MAX(updated_at) FROM profile").fetchone()[0]
        except sqlite3.Error:
            last = None
        return {"sessions": sessions, "last_mined": last}
    finally:
        db.close()


class Memory:
    """Facade the engine talks to: store, current session, profile, compaction and mining.

    All SQLite work happens off the event loop: turns are queued to a single writer, loads and
    profile reads run in worker threads.
    """

    def __init__(self, engine, path: str | Path | None = None, driver_id: str = "driver",
                 budget_tokens: int = DEFAULT_BUDGET_TOKENS, resume_s: float = RESUME_WINDOW_S,
                 defer: bool = False) -> None:
        self.engine = engine
        self.budget = int(budget_tokens)
        self._path = path
        self.driver_id = driver_id
        self._resume_s = resume_s
        self.store: SessionStore | None = None
        self.profile: ProfileStore | None = None
        self.session: Session | None = None
        self._turns_since_mine = 0
        self._mined_id = 0
        self._mining: asyncio.Task | None = None
        self._pending: list[tuple[int, list[dict]]] = []
        self._writer: asyncio.Task | None = None
        self._compacting: asyncio.Task | None = None
        engine.system_extra.append("")
        self._summary_slot = len(engine.system_extra) - 1
        engine.system_extra.append("")
        self._profile_slot = len(engine.system_extra) - 1
        engine.intercepts.insert(0, self.intercept)
        engine.closers.append(self.close)
        self.speak = self._speak
        if not defer:
            self._load_sync()

    # ── load ─────────────────────────────────────────────────────
    def _load_sync(self) -> None:
        store = SessionStore(self._path)
        profile = ProfileStore(store.path)
        last = store.last_session(self.driver_id)
        if last and time.time() - last.updated_at < self._resume_s:
            session = last
            self.engine.messages = store.load_messages(last.id, self.budget)
            self._mined_id = store.max_turn_id(last.id)
        else:
            session = store.open_session(self.driver_id)
        self.store, self.profile, self.session = store, profile, session
        self._set_summary_slot(store.summary(session.id))
        self._refresh_profile_line()

    async def load(self) -> None:
        await asyncio.to_thread(self._load_sync)

    # ── slots ────────────────────────────────────────────────────
    def _set_summary_slot(self, summary: str) -> None:
        self.engine.system_extra[self._summary_slot] = (SUMMARY_PREFIX + summary) if summary else ""

    def _refresh_profile_line(self) -> None:
        try:
            self.engine.system_extra[self._profile_slot] = profile_line(self.profile.load())
        except Exception:
            log.exception("profile unavailable")

    async def _refresh(self) -> None:
        await asyncio.to_thread(self._refresh_profile_line)

    # ── recording ────────────────────────────────────────────────
    def record_turn(self, start_idx: int, card: dict | None = None) -> None:
        """Queue the messages of one turn, from its user message at `start_idx` to the end.

        Builds rows in memory only; the write happens on the writer task. Never raises.
        """
        try:
            if self.store is None:
                return
            msgs = self.engine.messages
            if not (0 <= start_idx < len(msgs)) or msgs[start_idx].get("role") != "user":
                start_idx = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("role") == "user"), -1)
                if start_idx < 0:
                    return
            rows: list[dict] = []
            for m in msgs[start_idx:]:
                role = m.get("role")
                if role == "tool" and rows:
                    rows[-1]["tool_results"].append({"id": m.get("tool_call_id"), "content": m.get("content")})
                elif role in ("user", "assistant"):
                    rows.append({"role": role, "text": m.get("content"), "tool_results": [], "tool_calls": [
                        {"id": c["id"], "name": c["function"]["name"], "arguments": c["function"]["arguments"]}
                        for c in m.get("tool_calls") or []]})
            if card and rows:
                rows[-1]["cards"] = [card]
            self._enqueue(self.session.id, rows)
            self._turns_since_mine += 1
            if self._turns_since_mine >= MINE_EVERY_TURNS:
                self._turns_since_mine = 0
                self._mine_in_background()
            self.schedule_compact()
        except Exception:
            log.exception("could not record the turn")

    def record_text(self, role: str, text: str) -> None:
        self._enqueue(self.session.id, [{"role": role, "text": text}])

    def _enqueue(self, session_id: int, rows: list[dict]) -> None:
        self._pending.append((session_id, rows))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write_all_sync()
            return
        if self._writer is None or self._writer.done():
            self._writer = loop.create_task(self._drain())

    def _write_all_sync(self) -> None:
        while self._pending:
            sid, rows = self._pending.pop(0)
            self.store.record_rows(sid, rows)

    async def _drain(self) -> None:
        while self._pending:
            sid, rows = self._pending.pop(0)
            try:
                await asyncio.to_thread(self.store.record_rows, sid, rows)
            except Exception:
                log.exception("could not write the turn")

    async def flush(self, timeout: float = FLUSH_TIMEOUT_S) -> None:
        w = self._writer
        if w is not None and not w.done():
            try:
                await asyncio.wait_for(asyncio.shield(w), timeout)
            except Exception:
                log.exception("memory writer did not finish in time")

    # ── compaction ───────────────────────────────────────────────
    async def _plan_compact(self, messages: list[dict]):
        if estimate_tokens(messages) <= self.budget:
            return None
        summary = await asyncio.to_thread(self.store.summary, self.session.id)
        framed = ([{"role": "system", "content": SUMMARY_PREFIX + summary}] if summary else []) + messages
        out = await compact(framed, self.budget, self._summarise)
        kept = [m for m in out if not (m.get("role") == "system"
                                       and str(m.get("content") or "").startswith(SUMMARY_PREFIX))]
        new = next((str(m["content"])[len(SUMMARY_PREFIX):] for m in out if m not in kept), "")
        if not new or len(kept) == len(messages):
            return None
        return kept, new

    async def _upto(self, kept: list[dict]) -> int:
        await self.flush()
        kept_users = sum(1 for m in kept if m.get("role") == "user")
        return await asyncio.to_thread(self.store.user_turn_count, self.session.id) - kept_users

    async def _apply_compact(self, kept: list[dict], new: str) -> None:
        upto = await self._upto(kept)
        await asyncio.to_thread(self.store.set_summary, self.session.id, new, upto)
        self._set_summary_slot(new)

    async def compact(self, messages: list[dict]) -> list[dict]:
        plan = await self._plan_compact(messages)
        if plan is None:
            return messages
        await self._apply_compact(*plan)
        return plan[0]

    def schedule_compact(self) -> None:
        if self._compacting and not self._compacting.done():
            return
        if estimate_tokens(self.engine.messages) <= self.budget:
            return
        try:
            self._compacting = asyncio.get_running_loop().create_task(self._compact_bg())
        except RuntimeError:
            pass

    async def _compact_bg(self) -> None:
        try:
            sid = self.session.id
            snap = list(self.engine.messages)
            plan = await self._plan_compact(snap)
            if plan is None:
                return
            kept, new = plan
            upto = await self._upto(kept)
            cur = self.engine.messages
            if self.session.id != sid or len(cur) != len(snap) or any(a is not b for a, b in zip(cur, snap, strict=True)):
                return
            self.engine.messages = kept
            self._set_summary_slot(new)
            await asyncio.to_thread(self.store.set_summary, self.session.id, new, upto)
        except Exception:
            log.exception("compaction failed")

    async def _summarise(self, text: str) -> str:
        llm = self.engine.llm
        if llm is None:
            raise RuntimeError("no llm")
        msgs = [{"role": "system", "content": _SUMMARY_SYSTEM}, {"role": "user", "content": text}]

        async def call() -> str:
            out = ""
            async for ev in llm.stream(msgs, []):
                if ev[0] == "text":
                    out += ev[1]
            return out
        return await asyncio.wait_for(call(), SUMMARY_TIMEOUT_S)

    # ── profile mining ───────────────────────────────────────────
    def _mine_in_background(self) -> None:
        if self.engine.llm is None or (self._mining and not self._mining.done()):
            return
        try:
            self._mining = asyncio.get_running_loop().create_task(self.mine_now())
        except RuntimeError:
            pass

    async def mine_now(self, extract=None) -> None:
        if extract is None:
            llm = self.engine.llm
            if llm is None:
                return
            async def extract(turns):
                return await extract_facts(llm, turns)
        await self.flush()
        sid = self.session.id
        turns = await asyncio.to_thread(self.store.texts_since, sid, self._mined_id)
        if not turns:
            return
        try:
            await self.profile.mine(turns, extract)
        except Exception:
            log.exception("profile mining failed")
            return
        if self.session.id == sid:
            self._mined_id = turns[-1]["turn_id"]
        await self._refresh()

    async def close(self) -> None:
        if self._mining and not self._mining.done():
            self._mining.cancel()
        await self.flush()
        await self.mine_now()

    # ── forgetting ───────────────────────────────────────────────
    async def purge(self) -> None:
        """Drop every stored transcript and summary, empty the live history and start a new session."""
        tasks = [t for t in (self._mining, self._compacting) if t and not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=2.0)
        self._pending.clear()
        await self.flush()
        await asyncio.to_thread(self.store.purge, self.driver_id)
        self.session = await asyncio.to_thread(self.store.open_session, self.driver_id)
        self._mined_id = await asyncio.to_thread(self.store.max_turn_id)
        self._turns_since_mine = 0
        self.engine.messages = []
        self._set_summary_slot("")

    # ── voice verbs ──────────────────────────────────────────────
    async def intercept(self, text: str) -> bool:
        if self.profile is None:
            return False
        intent = await handle_memory_intent(text, self.profile)
        if intent is None:
            return False
        if intent.action in _PURGING:
            await self.purge()
        await self._refresh()
        await self.speak(intent.reply)
        return True

    async def _speak(self, text: str) -> None:
        eng = self.engine
        eng.emit("reply", text)
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait(text)
        q.put_nowait(None)
        try:
            await eng._speak_loop(q)
        except Exception:
            log.exception("could not speak")
        finally:
            if getattr(eng, "state", "") == "speaking":
                eng._set_state("idle", "")
