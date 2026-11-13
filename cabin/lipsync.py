"""Text → viseme timeline for lip sync.

Each viseme is two articulation targets the avatar shader understands:
    jaw    -0.35 (lips pressed, M/B/P) … 1.0 (wide open "ah")
    round  -1 (spread "ee") … +1 (rounded "oo")

Sources, best first:
  1. phoneme/char timings from the TTS engine  → add_phonemes()
  2. word timings (streaming text + duration)   → add_word()   (simulator, most cloud TTS)
  3. playing-audio envelope                     → blended into the jaw by LipSyncDriver

Coarticulation is not modelled here: the avatar runs a critically-damped
spring toward the current target, which blends neighbours naturally.
"""
from __future__ import annotations

import bisect
import collections
import math
import re
import time
from dataclasses import dataclass

# viseme classes (jaw, round)
V = {
    "AA": (0.95, 0.0),   # father, cat
    "EH": (0.55, -0.45), # bed
    "IY": (0.28, -0.85), # see
    "OW": (0.60, 0.75),  # go
    "UW": (0.25, 1.00),  # you, two
    "ER": (0.35, 0.40),  # her
    "MBP": (-0.35, 0.0), # lips together
    "FV": (0.08, -0.30), # teeth on lip
    "TH": (0.22, -0.15),
    "L":  (0.30, -0.10),
    "WR": (0.22, 0.85),
    "SZ": (0.12, -0.55),
    "CH": (0.20, 0.45),
    "KG": (0.35, -0.05),
    "TDN": (0.20, -0.20),
    "REST": (0.0, 0.0),
}

# greedy grapheme rules, longest first
_RULES = [
    ("igh", "IY"), ("ough", "OW"), ("tion", "CH"), ("oo", "UW"), ("ee", "IY"), ("ea", "IY"),
    ("ou", "OW"), ("ow", "OW"), ("oa", "OW"), ("ai", "EH"), ("ay", "EH"), ("ie", "IY"),
    ("er", "ER"), ("ir", "ER"), ("ur", "ER"), ("th", "TH"), ("sh", "CH"), ("ch", "CH"),
    ("ph", "FV"), ("wh", "WR"), ("ng", "KG"), ("ck", "KG"), ("qu", "WR"),
    ("a", "AA"), ("e", "EH"), ("i", "IY"), ("o", "OW"), ("u", "UW"), ("y", "IY"),
    ("m", "MBP"), ("b", "MBP"), ("p", "MBP"), ("f", "FV"), ("v", "FV"),
    ("w", "WR"), ("r", "WR"), ("l", "L"), ("s", "SZ"), ("z", "SZ"), ("c", "KG"),
    ("k", "KG"), ("g", "KG"), ("q", "KG"), ("x", "KG"), ("j", "CH"),
    ("t", "TDN"), ("d", "TDN"), ("n", "TDN"), ("h", "KG"),
]
_VOWELS = {"AA", "EH", "IY", "OW", "UW", "ER"}

_NUM = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
        "6": "six", "7": "seven", "8": "eight", "9": "nine"}


def word_visemes(word: str) -> list[str]:
    w = re.sub(r"\d", lambda m: _NUM[m.group()], word.lower())
    w = re.sub(r"[^a-z]", "", w)
    if w.endswith("e") and len(w) > 2 and w[-2] not in "aeiouy":
        w = w[:-1]                                   # silent final e
    out, i = [], 0
    while i < len(w):
        for g, v in _RULES:
            if w.startswith(g, i):
                if not out or out[-1] != v:
                    out.append(v)
                i += len(g)
                break
        else:
            i += 1
    return out


@dataclass(order=True)
class Key:
    t: float
    jaw: float
    rnd: float


class VisemeTrack:
    """Time-stamped articulation keys; sample(t) returns the target at t."""

    def __init__(self) -> None:
        self.keys: list[Key] = []

    def clear(self) -> None:
        self.keys.clear()

    def add_word(self, word: str, start: float, duration: float) -> None:
        vis = word_visemes(word)
        if not vis:
            return
        trailing = 0.0
        if word.endswith((".", ",", "—", "!", "?", ";", ":")):
            trailing = min(0.28, duration * 0.35)    # pause belongs to this word's slot
        speak = max(duration - trailing, 0.08)
        weights = [1.7 if v in _VOWELS else (0.8 if v == "MBP" else 1.0) for v in vis]
        total = sum(weights)
        t = start
        for v, wgt in zip(vis, weights):
            seg = speak * wgt / total
            j, r = V[v]
            # emphasise open vowels slightly on longer words
            self._add(t + seg * 0.35, j, r)
            t += seg
        self._add(start + speak + 0.02, *V["REST"]) if trailing else None

    def add_phonemes(self, items: list[tuple[str, float]]) -> None:
        """items: [(viseme_class, start_time), ...] from a TTS alignment."""
        for v, t in items:
            self._add(t, *V.get(v, V["REST"]))

    def end_at(self, t: float) -> None:
        self._add(t, *V["REST"])

    def _add(self, t: float, jaw: float, rnd: float) -> None:
        bisect.insort(self.keys, Key(t, jaw, rnd))

    def sample(self, t: float) -> tuple[float, float]:
        if not self.keys:
            return 0.0, 0.0
        i = bisect.bisect_right(self.keys, Key(t, 9, 9)) - 1
        if i < 0:
            return 0.0, 0.0
        k = self.keys[i]
        if t - k.t > 0.35:            # nothing new for a while → relax to rest
            return 0.0, 0.0
        return k.jaw, k.rnd

    def prune(self, before: float) -> None:
        i = bisect.bisect_left(self.keys, Key(before, -9, -9))
        if i > 1:
            del self.keys[: i - 1]


ENV_FLOOR_DB = -42.0       # output rms at/below this reads as a closed mouth
ENV_RANGE_DB = 26.0        # ... and this many dB above it as fully open
ENV_WEIGHT = 0.65          # jaw = ENV_WEIGHT * envelope + (1 - ENV_WEIGHT) * viseme jaw
ENV_FRESH_S = 0.08         # envelope reports older than this mean no audio is playing
ENV_SILENT = 0.03          # envelope below this: a pause, the mouth rests


def envelope_to_jaw(rms: float) -> float:
    """Map output rms (0..1 full scale) to a 0..1 jaw opening."""
    db = 20.0 * math.log10(max(rms, 1e-9))
    return min(1.0, max(0.0, (db - ENV_FLOOR_DB) / ENV_RANGE_DB))


def blend_jaw(env: float, viseme_jaw: float) -> float:
    """Audio envelope leads; the text viseme shapes it and closes lips on M/B/P."""
    if env < ENV_SILENT:
        return viseme_jaw * (1.0 - ENV_WEIGHT) if viseme_jaw < 0 else 0.0
    return ENV_WEIGHT * env + (1.0 - ENV_WEIGHT) * viseme_jaw


class LipSyncDriver:
    """Publishes jaw/round targets from the audio that is actually playing.

    `Player` reports (t_abs, rms) on the playback clock through `audio.position_listeners`; the
    envelope is applied when its timestamp is reached, and the word visemes (stamped on the same
    clock by `Player.enqueue`) are sampled at the same instant. Without playing audio (silent
    speech) the visemes alone drive the mouth. The avatar smooths at display rate.
    """

    def __init__(self, ctrl, parent=None, start_timer: bool = True) -> None:
        from . import audio
        self.ctrl = ctrl
        self.track = VisemeTrack()
        self._end = 0.0
        self._muted = True                       # closed until words arrive after a stop
        self._env_q: collections.deque[tuple[float, float]] = collections.deque(maxlen=128)
        self._env = 0.0
        self._env_t = -1e9
        self._timer = None
        audio.position_listeners.append(self.on_position)
        if start_timer:
            from PySide6.QtCore import QTimer
            self._timer = QTimer(parent or ctrl, interval=16, timeout=self.tick)
            self._timer.start()

    @staticmethod
    def now() -> float:
        return time.monotonic()

    def close(self) -> None:
        from . import audio
        if self.on_position in audio.position_listeners:
            audio.position_listeners.remove(self.on_position)
        if self._timer is not None:
            self._timer.stop()

    def on_position(self, t_abs: float, rms: float) -> None:
        """Audio-thread entry: queue a playback-clock envelope sample."""
        self._env_q.append((t_abs, rms))

    def add_word(self, word: str, start: float, duration: float) -> None:
        self._muted = False
        self.track.add_word(word, start, duration)
        self._end = max(self._end, start + duration)

    def add_phonemes(self, items: list[tuple[str, float]]) -> None:
        self._muted = False
        self.track.add_phonemes(items)
        if items:
            self._end = max(self._end, items[-1][1] + 0.15)

    def stop(self) -> None:
        self._muted = True
        self.track.clear()
        self._end = 0.0
        self._env_q.clear()
        self._env = 0.0
        self._env_t = -1e9
        self.ctrl.set_viseme(0.0, 0.0, False)

    def _advance_envelope(self, t: float) -> None:
        q = self._env_q
        target = None
        while q and q[0][0] <= t:
            self._env_t, rms = q.popleft()
            target = envelope_to_jaw(rms)
        if target is not None:
            self._env += (target - self._env) * (0.7 if target > self._env else 0.4)

    def tick(self, t: float | None = None) -> None:
        t = self.now() if t is None else t
        if self._muted:
            self.ctrl.set_viseme(0.0, 0.0, False)
            return
        self._advance_envelope(t)
        audible = t - self._env_t < ENV_FRESH_S
        live = audible or t < self._end + 0.25
        vj, rnd = self.track.sample(t) if live else (0.0, 0.0)
        if audible:
            jaw = blend_jaw(self._env, vj)
        else:
            jaw = vj if live else 0.0
            self._env = 0.0
        self.ctrl.set_viseme(jaw, rnd, live)
        self.track.prune(t - 1.0)
