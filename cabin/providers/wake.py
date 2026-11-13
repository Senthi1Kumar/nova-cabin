"""Wake phrase matching on a transcript.

``WakeMatcher.match`` accepts the phrase only at the start of the text and tolerates the spellings
a speech recogniser produces for it ("hey noah", "hay nova", "hi nova"). The word after the
greeting is compared by similarity, so a different name is rejected.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

GREETINGS = {"hey", "hay", "hi", "ok", "okay", "hello", "hei", "heya"}
NAME_SIMILARITY = 0.7
BARE_ALIASES = {"nowa", "noa", "novaa", "novah"}      # recogniser spellings accepted without a greeting
GREETING_SIMILARITY = 0.8
_WORD = re.compile(r"[\w']+")
_SEPARATORS = " \t,.!?;:-"


def _same(word: str, ref: str, threshold: float) -> bool:
    return word == ref or SequenceMatcher(None, word, ref).ratio() >= threshold


def split_phrases(setting: str) -> list[str]:
    """``WAKE_WORD`` holds one phrase, or several separated by ``;`` or ``|``."""
    return [p.strip().lower() for p in re.split(r"[;|]", setting or "") if p.strip()]


class WakeMatcher:
    def __init__(self, phrases: list[str]) -> None:
        self.phrases = [_WORD.findall(p.lower()) for p in phrases if p.strip()]
        self.phrases = [p for p in self.phrases if p]

    def __bool__(self) -> bool:
        return bool(self.phrases)

    def match(self, text: str) -> tuple[bool, str]:
        """``(True, remainder)`` when ``text`` starts with a wake phrase, else ``(False, text)``."""
        words = list(_WORD.finditer((text or "").lower()))
        for phrase in self.phrases:
            end = self._starts_with(words, phrase)
            if end is not None:
                return True, text[end:].lstrip(_SEPARATORS)
        return False, text

    def _starts_with(self, words: list, phrase: list[str]) -> int | None:
        """Index in the text just past the phrase, or None."""
        n = len(phrase)
        if len(words) >= n and all(self._word_ok(words[i].group(), phrase[i], i == 0 and n > 1)
                                   for i in range(n)):
            return words[n - 1].end()
        if n > 1 and words and (words[0].group() == phrase[-1] or
                                (phrase[-1] == "nova" and words[0].group() in BARE_ALIASES)):
            return words[0].end()                          # the bare name: "nova, play jazz"
        return None

    @staticmethod
    def _word_ok(word: str, ref: str, greeting: bool) -> bool:
        if greeting and ref in GREETINGS:
            return word in GREETINGS
        return _same(word, ref, GREETING_SIMILARITY if greeting else NAME_SIMILARITY)
