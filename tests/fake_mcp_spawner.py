"""MCP server that spawns a long-lived child on initialize, records its pid, then never answers tools/call."""
import json
import subprocess
import sys
import time

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open(sys.argv[1], "w") as f:
    f.write(str(child.pid))
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {}}) + "\n")
        sys.stdout.flush()
    elif msg.get("method") == "tools/call":
        time.sleep(60)
