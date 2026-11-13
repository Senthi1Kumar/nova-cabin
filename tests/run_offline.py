"""Run the live app against the local mock + fixtures.  python tests/run_offline.py [main.py args]"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
port = os.environ.get("MOCK_PORT", "8765")
srv = subprocess.Popen([sys.executable, str(ROOT / "tests/mock_openai.py"), port])
time.sleep(0.6)
os.environ.update(NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
                  OPENAI_API_KEY="test", OPENAI_BASE_URL=f"http://127.0.0.1:{port}/v1", LLM_PROVIDER="openai",
                  OPENAI_MODEL="mock-1", TTS_PROVIDER="openai", STT_PROVIDER="openai")
import offline_services
offline_services.install()
import main
sys.argv = ["main.py", "--env", str(ROOT / "tests" / "empty.env"), *sys.argv[1:]]
try:
    rc = main.main()
finally:
    srv.terminate()
sys.exit(rc)
