"""Research specialist: Exa through an MCP server, the built-in web search as the fallback."""
from __future__ import annotations

import asyncio
import logging

from ..services.mcp_client import McpClient, McpError, result_text
from .base import Agent, AgentRequest, AgentResult

log = logging.getLogger("agents.research")

MAX_DEEP_SEARCHES = 3
EXA_TOOL = "web_search_exa"
_DEEP_HINTS = ("research", "deep", "compare", "in depth", "in-depth", "everything about")


class ResearchAgent(Agent):
    description = ("Research specialist: current facts, news, prices and comparisons from the web. "
                   "Set context.deep=true for a multi-search answer with a short synthesis.")

    def __init__(self, engine) -> None:
        super().__init__("research", engine)
        self.mcp: McpClient | None = None

    # ── transport ─────────────────────────────────────────────
    def _mcp_client(self) -> McpClient | None:
        if self.mcp is None:
            cmd = self.engine.cfg.get("EXA_MCP_CMD").split()
            if not cmd:
                return None
            key = self.engine.cfg.get("EXA_API_KEY")
            self.mcp = McpClient(cmd, env={"EXA_API_KEY": key} if key else None)
        return self.mcp

    async def _one(self, query: str) -> dict:
        client = self._mcp_client()
        if client is not None:
            try:
                res = await client.call(EXA_TOOL, {"query": query, "numResults": 5})
                text = result_text(res)
                if text and not res.get("isError"):
                    return {"source": "exa", "query": query, "text": text[:2400]}
            except (McpError, OSError) as e:
                log.warning("exa via MCP failed (%s); using web search", e)
        out, _card, _nav = await self.engine.tools.run("web_search", {"query": query})
        if out.get("error"):
            raise RuntimeError(out["error"])
        return {"source": "web_search", "query": query, "answer": out.get("answer", ""),
                "results": out.get("results", [])}

    # ── public API ────────────────────────────────────────────
    async def search(self, query: str) -> AgentResult:
        return await self.run(AgentRequest(query))

    async def execute(self, request: AgentRequest) -> tuple[dict, str]:
        query = request.request.strip()
        deep = bool(request.context.get("deep")) or any(h in query.lower() for h in _DEEP_HINTS)
        if not deep:
            hit = await self._one(query)
            return {"searches": [hit]}, self._brief(hit)
        queries = [query]
        for extra in request.context.get("queries") or []:
            if len(queries) < MAX_DEEP_SEARCHES and str(extra).strip():
                queries.append(str(extra).strip())
        for suffix in (" latest", " overview"):
            if len(queries) < MAX_DEEP_SEARCHES and len(request.context.get("queries") or []) == 0:
                queries.append(query + suffix)
        outcomes = await asyncio.gather(*(self._one(q) for q in queries[:MAX_DEEP_SEARCHES]),
                                        return_exceptions=True)
        hits = [o for o in outcomes if isinstance(o, dict)]
        if not hits:
            raise next(o for o in outcomes if isinstance(o, BaseException))
        summary = await self._synthesise(query, hits)
        return {"searches": hits, "searches_run": len(hits)}, summary

    @staticmethod
    def _brief(hit: dict) -> str:
        if hit.get("answer"):
            return str(hit["answer"])[:400]
        if hit.get("results"):
            return "; ".join(str(r.get("title", "")) for r in hit["results"][:3])
        return str(hit.get("text", ""))[:400]

    async def _synthesise(self, query: str, hits: list[dict]) -> str:
        material = "\n\n".join(f"[{h['query']}] {self._brief(h)}" for h in hits)
        try:
            client = self.llm()
        except RuntimeError:
            return material[:600]
        messages = [
            {"role": "system", "content": "Answer in exactly two short sentences using only the notes given."},
            {"role": "user", "content": f"Question: {query}\n\nNotes:\n{material}"}]
        parts: list[str] = []
        try:
            async for kind, val in client.stream(messages):
                if kind == "text":
                    parts.append(val)
        except Exception as e:                                       # noqa: BLE001
            log.warning("synthesis failed: %s", type(e).__name__)
            return material[:600]
        return "".join(parts).strip() or material[:600]
