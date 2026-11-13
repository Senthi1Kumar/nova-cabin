"""Settings from .env (no external dependency) with process-env override."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROVIDERS = {
    #            base_url                                                key env              model env
    "openrouter": ("https://openrouter.ai/api/v1",                      "OPENROUTER_API_KEY", "OPENROUTER_MODEL"),
    "openai":     (None,                                                "OPENAI_API_KEY",     "OPENAI_MODEL"),
    "gemini":     ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "GEMINI_MODEL"),
    "xai":        ("https://api.x.ai/v1",                               "XAI_API_KEY",        "XAI_MODEL"),
    "anthropic":  ("https://api.anthropic.com/v1",                      "ANTHROPIC_API_KEY",  "ANTHROPIC_MODEL"),
    "groq":       ("https://api.groq.com/openai/v1",                    "GROQ_API_KEY",       "GROQ_MODEL"),
}
PROVIDER_LABEL = {"openrouter": "OpenRouter", "openai": "OpenAI", "gemini": "Gemini",
                  "xai": "Grok", "anthropic": "Claude", "groq": "Groq"}


def load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if v[:1] in "\"'" and v[-1:] == v[:1] and len(v) >= 2:
            v = v[1:-1]
        elif v.startswith("#"):
            v = ""                                  # empty value followed by a comment
        else:
            v = v.split(" #", 1)[0].split("\t#", 1)[0].strip()
        out[k.strip()] = v
    return out


@dataclass
class Settings:
    raw: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Settings":
        data = load_dotenv(env_file or ROOT / ".env")
        for k, v in os.environ.items():          # real env wins
            if k in data or k.endswith(("_API_KEY", "_TOKEN", "_MODEL", "_PROVIDER", "_URL")):
                data[k] = v
        return cls(data)

    # ── typed accessors ──
    def get(self, key: str, default: str = "") -> str:
        v = self.raw.get(key, "")
        return v if v != "" else default

    def num(self, key: str, default: float) -> float:
        try:
            return float(self.get(key, str(default)))
        except ValueError:
            return default

    def flag(self, key: str, default: bool = False) -> bool:
        return self.get(key, "true" if default else "false").lower() in ("1", "true", "yes", "on")

    # ── LLM ──
    def llm(self) -> dict | None:
        pref = self.get("LLM_PROVIDER", "openrouter").lower()
        order = [pref] + [p for p in PROVIDERS if p != pref]
        for p in order:
            if p not in PROVIDERS:
                continue
            base, key_env, model_env = PROVIDERS[p]
            key = self.get(key_env)
            if not key:
                continue
            if p == "openai":
                base = self.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
            model = self.get("LLM_MODEL") if p == pref and self.get("LLM_MODEL") else self.get(model_env)
            return {"provider": p, "label": PROVIDER_LABEL[p], "base_url": base.rstrip("/"),
                    "api_key": key, "model": model,
                    "temperature": self.get("LLM_TEMPERATURE"),
                    "reasoning_effort": self.get("LLM_REASONING_EFFORT")}
        return None

    @property
    def metric(self) -> bool:
        return self.get("UNITS", "imperial").lower() == "metric"

    def summary(self) -> dict[str, str]:
        llm = self.llm()
        return {"llm": f"{llm['label']} · {llm['model']}" if llm else "none"}
