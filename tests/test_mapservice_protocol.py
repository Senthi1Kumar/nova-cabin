"""Map service control socket and shared-memory frames, with a fake service process."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cabin.nav.mapservice import MapServiceClient


def test_protocol_roundtrip(tmp_path):
    shm = tmp_path / "map.shm"
    sock = tmp_path / "map.sock"
    proc = subprocess.Popen(
        [sys.executable, "-m", "cabin.nav.mapservice", "--fake", "--shm", str(shm), "--sock", str(sock)],
        cwd=Path(__file__).resolve().parents[1])
    client = MapServiceClient(shm, sock)
    try:
        client.connect()
        _seq, _w, _h, _stride, pixels = client.read()
        assert pixels and any(pixels)
        first = client.read()[0]
        time.sleep(0.2)
        second = client.read()[0]
        assert second > first
        client.pause()
        time.sleep(0.15)
        held = client.read()[0]
        time.sleep(0.2)
        assert client.read()[0] == held
    finally:
        client.close()
        proc.terminate()
        proc.wait(timeout=3)


def test_link_pauses_on_thermal_flag_and_reports_map_live():
    from types import SimpleNamespace

    from cabin.config import Settings
    from cabin.nav.mapservice import MapServiceLink

    class Client:
        def __init__(self):
            self.calls = []

        def connect(self):
            self.calls.append("connect")

        def pause(self):
            self.calls.append("pause")

        def resume(self):
            self.calls.append("resume")

        def close(self):
            self.calls.append("close")

    def make(flag):
        eng = SimpleNamespace(cfg=Settings({"MAP_SERVICE": flag}), thermal_flags={}, events=[])
        eng.emit = lambda k, v: eng.events.append((k, v))
        return eng

    eng = make("off")
    link = MapServiceLink(eng, Client())
    link.start()
    assert eng.events == [("map_live", False)]

    eng = make("on")
    client = Client()
    link = MapServiceLink(eng, client)
    link.running = True
    assert link.sync() is True
    eng.thermal_flags = {"map_service_off": True}
    assert link.sync() is False
    eng.thermal_flags = {"map_service_off": False}
    assert link.sync() is True
    assert client.calls == ["pause", "resume"]
    assert [v for k, v in eng.events] == [True, False, True]

    dead = make("on")
    link = MapServiceLink(dead, Client())
    assert link.sync() is False


def test_read_retries_when_frame_changes_mid_copy(tmp_path):
    from cabin.nav import mapservice as ms

    class Torn(bytearray):
        reads = 0

        def __getitem__(self, key):
            out = super().__getitem__(key)
            Torn.reads += 1
            if Torn.reads == 1:
                ms.HEADER.pack_into(self, 0, 2, 2, 1, 8, 0, 0)
            return out

    buf = Torn(ms.HEADER.size + 8)
    ms.HEADER.pack_into(buf, 0, 1, 2, 1, 8, 0, 0)
    client = ms.MapServiceClient(tmp_path / "x", tmp_path / "y")
    client._mm = buf
    seq, _w, _h, _s, data = client.read()
    assert Torn.reads == 2 and seq == 2 and len(data) == 8
