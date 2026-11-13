"""Transcript gate: never act on a garbled transcript.

``check`` returns ``drop`` for noise-only transcripts (a stray "yet" or "uh"), ``retry`` when the
recogniser's confidence is low (the assistant asks the driver to repeat) and ``ok`` otherwise.

Parakeet exposes no per-token score through DriveOS, so ``estimate_confidence`` derives one from how
plausible the transcript length is for the audio duration: conversational speech runs about 1 to 5
words per second, and a transcript far outside that band usually means the decoder hallucinated words
over noise or dropped most of the utterance.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Literal

ASR_RETRY_PROMPT = "Sorry, say that again?"
MIN_CONFIDENCE = 0.5

FILLERS = frozenset({"yet", "uh", "um", "hmm", "the", "a", "you", "and", "so", "uh-huh", "mm", "huh", "oh", "ah", "eh"})
SHORT_COMMANDS = frozenset({
    "yes", "no", "stop", "cancel", "okay", "ok", "approve", "back", "next", "one", "two", "three", "four", "five",
    "six", "seven", "eight", "nine", "ten", "zero",
})
SHORT_CONTENT = frozenset({"gas", "atm", "bar", "tea", "map", "car", "pay", "eat", "bus", "gym", "spa", "pub", "inn"})
FUZZY = 0.84
_WORD = re.compile(r"[\w'-]+")

Verdict = Literal["ok", "retry", "drop"]


def _words(text: str) -> list[str]:
    return [w.lower().strip("'-") for w in _WORD.findall(text)]


def estimate_confidence(text: str, audio_s: float) -> float:
    """1.0 for a plausible speaking rate, falling toward 0 as the rate leaves 0.7 to 6.5 words per second."""
    n = len(_words(text))
    if n == 0:
        return 0.0
    audio_s = max(audio_s, 0.2)
    rate = n / audio_s
    if rate > 6.5:
        return max(0.0, 1.0 - (rate - 6.5) / 4.0) * 0.5
    if rate < 0.7 and n >= 3:
        return max(0.0, 1.0 - (0.7 - rate) / 0.5) * 0.5
    return 1.0


def _mentions(words: list[str], vocabulary) -> bool:
    """True when the transcript contains a vocabulary term, exactly or within a small spelling distance."""
    for term in vocabulary or ():
        tw = _words(term)
        if not tw or len(tw) > len(words):
            continue
        joined = " ".join(tw)
        for i in range(len(words) - len(tw) + 1):
            if SequenceMatcher(None, " ".join(words[i:i + len(tw)]), joined).ratio() >= FUZZY:
                return True
    return False


class TranscriptGate:
    def __init__(self, min_confidence: float = MIN_CONFIDENCE) -> None:
        self.min_confidence = min_confidence

    def check(self, text: str, confidence: float, vocabulary=()) -> Verdict:
        words = _words(text)
        if not words:
            return "drop"
        if all(w in SHORT_COMMANDS or w.isdigit() for w in words):
            return "ok"
        content = [w for w in words if w not in FILLERS]
        if not content:
            return "drop"
        if _mentions(words, vocabulary):
            return "ok"
        substantive = len(content) >= 2 or len(content[0]) >= 4 or content[0] in SHORT_CONTENT
        if not substantive:
            return "drop"
        return "ok" if confidence >= self.min_confidence else "retry"
