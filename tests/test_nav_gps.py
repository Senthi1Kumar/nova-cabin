"""GpsFeed: real fixes update the vehicle, no fix falls back to the simulator with a badge."""
from __future__ import annotations

import asyncio
import functools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest

from cabin.config import Settings
from cabin.nav.gps import GpsFeed
from cabin.tools import Vehicle

GPX = """<?xml version="1.0"?><gpx version="1.1"><trk><trkseg>
<trkpt lat="10.8000" lon="78.7000"><time>2026-01-01T00:00:00Z</time></trkpt>
<trkpt lat="10.8010" lon="78.7000"><time>2026-01-01T00:00:01Z</time></trkpt>
<trkpt lat="10.8020" lon="78.7000"><time>2026-01-01T00:00:02Z</time></trkpt>
</trkseg></trk></gpx>"""


@pytest.fixture(autouse=True)
def _own_data_dir(tmp_path, monkeypatch):
    """Each feed starts cold: no last-known fix from an earlier test."""
    monkeypatch.setenv("NOVA_CABIN_DATA", str(tmp_path))


def test_last_fix_is_remembered_across_starts(tmp_path):
    from cabin.nav.gps import _load_last_fix, _save_last_fix
    path = tmp_path / "last_fix.json"
    assert _load_last_fix(path) is None
    _save_last_fix(path, 10.9123456, 78.7012345)
    assert _load_last_fix(path) == (10.912346, 78.701234)
    v = Vehicle(name="t", lat=37.0, lon=-122.0, place="x", battery_pct=80, range_full_mi=300, cabin_temp_f=70)
    feed = GpsFeed(Settings.load(tmp_path / "none.env"), v, lambda k, val: None, spec="gpsd")
    feed._to_sim()
    assert (v.lat, v.lon) == (10.912346, 78.701234)
    assert v.fix_state == "last"


def arun(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        return asyncio.run(fn(*a, **k))
    return wrapper


def vehicle() -> Vehicle:
    return Vehicle(name="t", lat=37.0, lon=-122.0, place="x", battery_pct=80, range_full_mi=300, cabin_temp_f=70)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    async def sleep(self, dt):
        self.t += dt
        await asyncio.sleep(0)


@arun
async def test_gps_no_fix_falls_back_with_badge():
    clock, events, v = FakeClock(), [], vehicle()

    async def opener(spec):
        async def silent():
            while True:
                await asyncio.sleep(3600)
                yield False
        return "fake", silent()

    cfg = Settings({"GPS_FIX_TIMEOUT_S": "30"})
    feed = GpsFeed(cfg, v, lambda k, p: events.append((k, p)), spec="gpsd", opener=opener,
                   clock=clock, sleep=clock.sleep)
    feed.start()
    assert v.fix_state == "no_fix"
    for _ in range(300):
        await asyncio.sleep(0)
    assert v.fix_state == "sim"
    assert clock.t >= 30
    gps = [p for k, p in events if k == "gps"]
    assert gps[-1]["badge"] == "Simulated position"
    assert (v.lat, v.lon) == (37.0, -122.0)
    await feed.stop()


@arun
async def test_a_coarse_fix_does_not_leave_the_simulated_place():
    from driveos.nav.gps import GpsFix
    v = vehicle()

    async def opener(_spec):
        async def one():
            yield GpsFix(1.0, 2.0, 0.0, None, accuracy_m=5000.0)
            await asyncio.sleep(3600)
        return "gpsd", one()

    feed = GpsFeed(Settings({}), v, lambda *_a: None, spec="gpsd", opener=opener)
    feed.start()
    for _ in range(40):
        await asyncio.sleep(0)
    await feed.stop()
    assert v.fix_state != "fix"
    assert (v.lat, v.lon) == (37.0, -122.0)


@arun
async def test_gps_replay_updates_vehicle(tmp_path):
    from driveos.nav.gps import open_source
    f = tmp_path / "d.gpx"
    f.write_text(GPX)
    events, v = [], vehicle()
    feed = GpsFeed(Settings({}), v, lambda k, p: events.append((k, p)), spec=f"replay:{f}@50",
                   opener=open_source)
    feed.start()
    for _ in range(200):
        if v.fix_state == "fix" and v.lat > 10.8015:
            break
        await asyncio.sleep(0.02)
    await feed.stop()
    assert v.fix_state == "fix"
    assert abs(v.lon - 78.7) < 1e-6 and 10.8 < v.lat < 10.81
    assert v.heading < 1 or v.heading > 359
    gps = [p for k, p in events if k == "gps"]
    assert gps[-1]["fix_state"] == "fix" and gps[-1]["badge"] == "Live GPS"
    assert any(k == "vehicle" for k, _ in events)


@arun
async def test_auto_tries_gpsd_then_serial():
    tried, v = [], vehicle()

    async def opener(spec):
        tried.append(spec)
        if spec == "gpsd":
            raise ConnectionRefusedError("no gpsd")

        async def one():
            from driveos.nav.gps import GpsFix
            yield GpsFix(10.8, 78.7, 0.0, None)
            await asyncio.sleep(3600)
        return "GPS ttyUSB0", one()

    events = []
    feed = GpsFeed(Settings({}), v, lambda k, p: events.append((k, p)), spec="auto", opener=opener)
    feed.start()
    for _ in range(100):
        if v.fix_state == "fix":
            break
        await asyncio.sleep(0.01)
    await feed.stop()
    assert tried == ["gpsd", "serial:/dev/ttyUSB0@9600"]
    assert v.fix_state == "fix" and feed.source == "GPS ttyUSB0"
    assert any(k == "gps" and p["fix_state"] == "fix" for k, p in events)


@arun
async def test_sim_spec_never_opens_a_source():
    v = vehicle()
    feed = GpsFeed(Settings({}), v, lambda k, p: None, spec="sim")
    feed.start()
    assert v.fix_state == "sim"
    await feed.stop()


@arun
async def test_throttle_flushes_last_fix():
    from driveos.nav.gps import GpsFix
    clock, events, v = FakeClock(), [], vehicle()
    lats = [10.80 + i * 1e-4 for i in range(5)]

    async def opener(spec):
        async def burst():
            for lat in lats:
                yield GpsFix(lat, 78.7, 5.0, 0.0)
                clock.t += 0.04
                await asyncio.sleep(0)
            await asyncio.sleep(3600)
        return "fake", burst()

    feed = GpsFeed(Settings({}), v, lambda k, p: events.append((k, p)), spec="gpsd", opener=opener,
                   clock=clock, sleep=clock.sleep)
    feed.start()
    for _ in range(12):
        await asyncio.sleep(0)
    await feed.stop()
    gps = [p for k, p in events if k == "gps"]
    assert gps[-1]["lat"] == lats[-1]
    assert len(gps) < 5


@arun
async def test_returns_to_live_after_sim():
    from driveos.nav.gps import GpsFix
    clock, events, v = FakeClock(), [], vehicle()
    go = asyncio.Event()

    async def opener(spec):
        async def late():
            await go.wait()
            yield GpsFix(10.8, 78.7, 0.0, None)
            await asyncio.sleep(3600)
        return "fake", late()

    feed = GpsFeed(Settings({"GPS_FIX_TIMEOUT_S": "30"}), v, lambda k, p: events.append((k, p)), spec="gpsd",
                   opener=opener, clock=clock, sleep=clock.sleep)
    feed.start()
    for _ in range(300):
        await asyncio.sleep(0)
    assert v.fix_state == "sim"
    go.set()
    for _ in range(10):
        await asyncio.sleep(0)
    await feed.stop()
    assert v.fix_state == "fix"
    gps = [p for k, p in events if k == "gps"]
    assert gps[-1]["badge"] == "Live GPS"


@arun
async def test_stop_is_idempotent():
    feed = GpsFeed(Settings({}), vehicle(), lambda k, p: None, spec="sim")
    feed.start()
    await feed.stop()
    await feed.stop()


def test_lost_fix_keeps_the_puck_in_place(tmp_path):
    v = Vehicle(name="t", lat=37.0, lon=-122.0, place="x", battery_pct=80, range_full_mi=300, cabin_temp_f=70)
    feed = GpsFeed(Settings.load(tmp_path / "none.env"), v, lambda k, val: None, spec="gpsd")
    feed._last_fix = 1.0
    v.lat, v.lon = 10.91, 78.70
    feed._to_sim()
    assert (v.lat, v.lon) == (10.91, 78.70)
    assert v.fix_state == "last"
