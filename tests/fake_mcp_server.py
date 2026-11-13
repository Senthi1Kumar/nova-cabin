"""Tiny MCP server over stdio for tests: initialize, then tools/call echoes a canned search result."""
import json
import sys

for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg["method"] == "initialize":
        res = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake", "version": "0"}}
    elif msg["method"] == "tools/call":
        q = msg["params"]["arguments"].get("query", "")
        res = {"content": [{"type": "text", "text": f"exa result for {q}"}]}
    else:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "no"}}) + "\n")
        sys.stdout.flush()
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}) + "\n")
    sys.stdout.flush()
