import numpy as np

from cabin import audio


def test_muted_player_never_opens_stream(monkeypatch):
    monkeypatch.setenv("NOVA_AUDIO_MUTE", "1")

    def boom(*a, **k):
        raise AssertionError("OutputStream opened while muted")

    monkeypatch.setattr(audio, "sd", type("SD", (), {"OutputStream": boom,
                                                     "query_devices": staticmethod(lambda *a, **k: {"default_samplerate": 24000})}))
    p = audio.Player()
    assert not p.available
    chunk = np.zeros(2400, dtype=np.float32)
    t1 = p.enqueue(chunk)
    t2 = p.enqueue(chunk)
    assert t2 > t1
