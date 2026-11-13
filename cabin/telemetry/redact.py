"""Redaction applied at the sink: keys, PAN-like runs, OTP codes, street addresses, raw biometric scores.

GPS fixes and place names are never touched. ``redact`` returns a new object; strings are scrubbed,
dict values are dropped by key name, and registered secret values are removed wherever they appear.
"""
from __future__ import annotations

import re
import threading
from typing import Any

MASK = "[redacted]"

_SECRET_KEY = re.compile(
    r"(?i)^(?:.*[_-])?(?:api[_-]?key|apikey|token|secret|password|passwd|authorization|bearer|credentials?)$")
_TEMPLATE_KEY = re.compile(r"(?i)^(?:.*[_-])?(?:template|embedding|voiceprint|enrol(?:l)?ment)$")
_SCORE_KEY = re.compile(r"(?i)(?:^|[_-])(?:score|scores|llr|cosine|similarity|logit)(?:$|[_-])")
_BIO_CTX = re.compile(r"(?i)biometric|verify|verif")
_CTX_VALUE_KEYS = ("kind", "name", "type", "stage", "tool", "event", "event_type", "agent")

_KEY_PATTERNS = [
    re.compile(r"""\b[\w-]*(?:api[_-]?key|token|secret|password)["']?\s*[=:]\s*["']?[^\s&"',]+""", re.IGNORECASE),
    re.compile(r"\bsk-or-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\b(?:pk|sk|tk)\.ey[\w.\-]+"),
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9+/=._\-]{8,}"),
]
_BIO_TEXT = re.compile(r"(?i)verify|verif|biometric|voice\s*id|face\s*id")
_SCORE_TEXT = re.compile(r"(?i)\b(score|llr|cosine)(\s*[=:]\s*)-?[\d.]+")
_PAN = re.compile(r"(?<![\d.])(?:\d[ -]?){12,18}\d(?![\d])")
_DIGITS = r"\d(?:[ -]?\d){3,7}"
_OTP_AFTER = re.compile(rf"(?i)(\b(?:code|otp|pin)\b[^\d]{{0,20}})(?<!\d){_DIGITS}(?!\d)")
_OTP_BEFORE = re.compile(rf"(?i)(?<!\d){_DIGITS}(?!\d)([^\d]{{0,20}}\b(?:code|otp|pin)\b)")
_STREET = (r"Road|Rd|Street|St|Salai|Nagar|Main|Cross|Layout|Avenue|Ave|Lane|Ln|Drive|Dr|"
           r"Boulevard|Blvd")
_ADDRESS = re.compile(
    rf"(?<![\d.])\d{{1,5}}[A-Za-z]?\s+(?:[\w.'\-]+\s+){{0,3}}?(?:{_STREET})\b\.?", re.IGNORECASE)

_lock = threading.Lock()
_secrets: set[str] = set()


def add_secret(value: str) -> None:
    if value and len(value) >= 8:
        with _lock:
            _secrets.add(value)


def clear_secrets() -> None:
    with _lock:
        _secrets.clear()


def _scrub(text: str) -> str:
    with _lock:
        known = sorted(_secrets, key=len, reverse=True)
    for s in known:
        if s in text:
            text = text.replace(s, "[redacted:key]")
    for pat in _KEY_PATTERNS:
        text = pat.sub("[redacted:key]", text)
    text = _PAN.sub("[redacted:pan]", text)
    text = _OTP_AFTER.sub(lambda m: m.group(1) + "[redacted:otp]", text)
    text = _OTP_BEFORE.sub(lambda m: "[redacted:otp]" + m.group(1), text)
    if _BIO_TEXT.search(text):
        text = _SCORE_TEXT.sub(lambda m: m.group(1) + m.group(2) + "[redacted:biometric]", text)
    return _ADDRESS.sub("[redacted:address]", text)


def scrub_text(text: str) -> str:
    return _scrub(text)


def _bio_context(d: dict) -> bool:
    for k, v in d.items():
        if isinstance(k, str) and _BIO_CTX.search(k):
            return True
        if k in _CTX_VALUE_KEYS and isinstance(v, str) and _BIO_CTX.search(v):
            return True
    return False


def redact(obj: Any, context: str = "") -> Any:
    """Return ``obj`` with secrets and personal data masked. ``context`` names the enclosing operation."""
    return _walk(obj, bool(_BIO_CTX.search(context or "")))


def _walk(obj: Any, bio: bool) -> Any:
    if isinstance(obj, str):
        return _scrub(obj)
    if isinstance(obj, dict):
        here = bio or _bio_context(obj)
        out = {}
        for k, v in obj.items():
            ks = k if isinstance(k, str) else str(k)
            if _SECRET_KEY.match(ks) and v not in (None, "", False):
                out[k] = MASK
            elif _TEMPLATE_KEY.match(ks) or here and _SCORE_KEY.search(ks) and isinstance(v, (int, float, list, tuple)):
                out[k] = "[redacted:biometric]"
            else:
                out[k] = _walk(v, here)
        return out
    if isinstance(obj, (list, tuple)):
        return [_walk(v, bio) for v in obj]
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, (bytes, bytearray)):
        return _scrub(bytes(obj).decode("utf-8", "replace"))
    if isinstance(obj, (set, frozenset)):
        return [_walk(v, bio) for v in obj]
    return _scrub(str(obj))
