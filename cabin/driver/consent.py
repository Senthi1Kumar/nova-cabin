"""Recording and camera consent, stored once per kind next to the biometric data (0600)."""
from __future__ import annotations

import json
import os
from pathlib import Path

KINDS = ("voice", "face")


class ConsentStore:
    def __init__(self, home: Path):
        self.path = Path(home) / "driver_consent.json"

    def _read(self) -> dict:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def get(self, kind: str) -> bool:
        return self._read().get(kind) is True

    def all(self) -> dict[str, bool]:
        data = self._read()
        return {k: data.get(k) is True for k in KINDS}

    def set(self, kind: str, value: bool) -> None:
        if kind not in KINDS:
            raise ValueError("unknown consent kind")
        data = self._read()
        data[kind] = bool(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)
