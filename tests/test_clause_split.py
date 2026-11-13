import asyncio

import numpy as np

from cabin.audio import TTS_SR
from cabin.providers.tts_phonon import PhononTTS
from cabin.tts import split_clauses


def test_split_em_dash():
    assert split_clauses("It's close — two minutes away.") == [("It's close", 220), ("two minutes away.", 0)]


def test_split_semicolon_colon_ellipsis():
    assert split_clauses("One; two: three… four.", 100) == [("One", 100), ("two", 100), ("three", 100), ("four.", 0)]


def test_spaced_hyphen_splits_but_compound_does_not():
    assert split_clauses("Close - two minutes.") == [("Close", 220), ("two minutes.", 0)]
    assert split_clauses("A well-known cafe.") == [("A well-known cafe.", 0)]


def test_no_punctuation_single_clause():
    assert split_clauses("Hello there.") == [("Hello there.", 0)]


def test_colon_in_time_not_split():
    assert split_clauses("Leave at 10:30 today.") == [("Leave at 10:30 today.", 0)]


def test_empty():
    assert split_clauses("") == []


class FakeStream:
    sample_rate = TTS_SR

    def __init__(self, n):
        self.n = n

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        yield np.full(self.n, 0.1, np.float32)


class FakeModel:
    def stream(self, text, voice=None):
        return FakeStream(TTS_SR // 2)


def test_synth_inserts_silence_and_keeps_timings_continuous(monkeypatch):
    monkeypatch.setenv("TTS_CLAUSE_PAUSE_MS", "200")
    tts = PhononTTS(model=FakeModel())
    sp = asyncio.run(tts.synth("It's close — two minutes away."))
    pcm = np.frombuffer(sp.pcm16, np.int16)
    assert len(pcm) == TTS_SR // 2 * 2 + int(TTS_SR * 0.2)
    assert not pcm[TTS_SR // 2:TTS_SR // 2 + int(TTS_SR * 0.2)].any()
    starts = [s for _, s, _ in sp.words]
    assert starts == sorted(starts)
    assert [w for w, _, _ in sp.words] == ["It's", "close", "two", "minutes", "away."]
    assert sp.words[2][1] >= 0.5 + 0.2 - 1e-6          # third word starts after the pause
