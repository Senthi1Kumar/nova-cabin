"""Minimal MCP client: JSON-RPC 2.0 over a subprocess's stdio (initialize, then tools/call)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal

log = logging.getLogger("mcp")

PROTOCOL_VERSION = "2024-11-05"
DEFAULT_TIMEOUT_S = 8.0
KILL_GRACE_S = 1.0


class McpError(RuntimeError):
    pass


def _signal_group(proc, sig) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, sig)


async def _reap(proc) -> None:
    """Stop the whole process group: SIGTERM, then SIGKILL after a short grace."""
    _signal_group(proc, signal.SIGTERM)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), KILL_GRACE_S)
    _signal_group(proc, signal.SIGKILL)          # also catches children that outlived the leader
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), KILL_GRACE_S)


class McpClient:
    """One short-lived server process per ``call``; the whole exchange is bounded by ``timeout_s``."""

    def __init__(self, cmd: list[str], env: dict[str, str] | None = None, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        if not cmd:
            raise McpError("empty MCP command")
        self.cmd = list(cmd)
        self.env = env
        self.timeout_s = timeout_s

    async def call(self, tool: str, args: dict) -> dict:
        try:
            return await asyncio.wait_for(self._session(tool, args), self.timeout_s)
        except TimeoutError as e:
            raise McpError(f"MCP call '{tool}' timed out after {self.timeout_s:g}s") from e

    async def _session(self, tool: str, args: dict) -> dict:
        env = {**os.environ, **(self.env or {})}
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=True)
        except OSError as e:
            raise McpError(f"cannot start MCP server: {type(e).__name__}") from e
        try:
            await self._request(proc, 1, "initialize", {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "nova-cabin", "version": "1"}})
            await self._send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            return await self._request(proc, 2, "tools/call", {"name": tool, "arguments": args})
        finally:
            with contextlib.suppress(Exception):
                if proc.stdin:
                    proc.stdin.close()
            # shielded so a second cancellation cannot skip the cleanup
            await asyncio.shield(_reap(proc))

    @staticmethod
    async def _send(proc, msg: dict) -> None:
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        await proc.stdin.drain()

    async def _request(self, proc, rid: int, method: str, params: dict) -> dict:
        await self._send(proc, {"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise McpError("MCP server closed the stream")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") != rid:
                continue
            if "error" in msg:
                err = msg["error"]
                raise McpError(str(err.get("message", err) if isinstance(err, dict) else err)[:200])
            result = msg.get("result")
            return result if isinstance(result, dict) else {}


def result_text(result: dict) -> str:
    """Concatenate the text parts of a tools/call result."""
    parts = result.get("content") or []
    return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text").strip()
