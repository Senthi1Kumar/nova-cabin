"""Front-end worker: push never blocks; Smart Turn runs off the event loop."""
import asyncio
import threading
import time

import numpy as np

from cabin.audio import VAD, VADConfig
from cabin.config import Settings
from cabin.frontend import Context, FrontEnd


class SlowStage:
    def __init__(self, delay=0.005):
        self.delay = delay
        self.n = 0

    def process(self, frame, ctx: Context):
        time.sleep(self.delay)
        ctx.emit("level", float(self.n))
        self.n += 1
        return frame


def test_worker_never_blocks_push():
    got = []
    fe = FrontEnd(Settings({}), got.append, stages=[SlowStage()])
    fe.start()
    frame = np.zeros(320, np.float32)
    took = []
    for _ in range(500):
        t = time.perf_counter()
        fe.push(frame)
        took.append(time.perf_counter() - t)
        time.sleep(0.0005)
    deadline = time.time() + 5
    while time.time() < deadline and len(got) + fe.stats["dropped"] < 500:
        time.sleep(0.01)
    fe.stop()
    assert sorted(took)[-3] < 0.001      # allow for OS scheduling stalls on a loaded host
    levels = [e.level for e in got]
    assert levels == sorted(levels) and len(got) + fe.stats["dropped"] == 500
    assert fe.stats["dropped"] > 0


def test_stage_none_drops_frame_and_stats():
    class Drop:
        def process(self, frame, ctx):
            return None

    seen = SlowStage(0)
    fe = FrontEnd(Settings({}), lambda e: None, stages=[Drop(), seen])
    fe.process(np.zeros(320, np.float32), None, 0.0)
    assert seen.n == 0 and fe.stats["frames"] == 1


class FakeSeg:
    class cfg:
        threshold = 0.5

    def __init__(self):
        self.n = 0
        self.last_prob = 0.0

    def reset(self):
        self.n = 0

    def resume(self):
        pass

    def feed(self, pcm):
        self.n += 1
        self.last_prob = 1.0 if self.n <= 12 else 0.0
        if self.n == 20:
            return [("pause", np.zeros(8000, np.float32))]
        return []


class SlowAnalyzer:
    def probability(self, audio):
        time.sleep(0.2)
        return 0.9


def test_smart_turn_runs_off_event_loop():
    from cabin.providers.turn import SileroTurn

    async def run():
        v = VAD(VADConfig(), engine="energy", threaded=True)
        v._turn = SileroTurn(v.cfg, v, segmenter=FakeSeg(), analyzer=SlowAnalyzer())
        worst = 0.0
        stop = False

        async def ticker():
            nonlocal worst
            while not stop:
                t = time.perf_counter()
                await asyncio.sleep(0.005)
                worst = max(worst, time.perf_counter() - t - 0.005)

        task = asyncio.ensure_future(ticker())
        events = []
        frame = np.full(320, 0.1, np.float32)
        for _ in range(60):
            ev, _lvl = v.update(frame, None)
            if ev:
                events.append(ev)
            await asyncio.sleep(0.02)
        stop = True
        await task
        v.stop()
        return worst, events

    worst, events = asyncio.run(run())
    assert events[:2] == ["start", "end"]
    assert worst < 0.02


def test_vad_is_synchronous_when_flag_off():
    async def run():
        v = VAD(VADConfig(), engine="energy", threaded=False)
        v.update(np.zeros(320, np.float32), None)
        return v._frontend

    assert asyncio.run(run()) is None


def _loud():
    t = np.arange(320) / 16000
    return (0.2 * np.sin(2 * np.pi * 170 * t)).astype(np.float32)


def _wait(cond, secs=5.0):
    end = time.time() + secs
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


def test_end_delivered_when_queue_overflows():
    class Slow:
        def process(self, frame, ctx):
            time.sleep(0.003)
            return frame

    async def run():
        v = VAD(VADConfig(end_silence_ms=200), engine="energy", threaded=True)
        v._ensure_worker()
        v._frontend.stages.insert(0, Slow())
        for _ in range(300):                       # flood well past the queue size
            v.update(_loud(), None)
        events = []
        for _ in range(400):
            ev, _lvl = v.update(np.zeros(320, np.float32), None)
            if ev:
                events.append(ev)
            await asyncio.sleep(0.005)
        dropped = v._frontend.stats["dropped"]
        v.stop()
        return events, dropped

    events, dropped = asyncio.run(run())
    assert dropped > 0
    assert "start" in events and "end" in events


def test_reset_mid_utterance_drops_stale_events():
    async def run():
        v = VAD(VADConfig(), engine="energy", threaded=True)
        v.update(np.zeros(320, np.float32), None)
        fe = v._frontend
        fe.stages.insert(0, SlowStage(0.01))
        for _ in range(30):
            v.update(_loud(), None)
        v.reset()
        assert fe.reset_seq > 30
        await asyncio.sleep(0.5)
        leftover = list(v._events)
        started = v.in_speech
        v.stop()
        return leftover, started

    leftover, started = asyncio.run(run())
    assert leftover == []
    assert started is False


def test_worker_starts_via_call_soon_threadsafe():
    async def run():
        v = VAD(VADConfig(), engine="energy", threaded=True)
        loop = asyncio.get_running_loop()
        done = loop.create_future()

        def cb():
            v.update(np.zeros(320, np.float32), None)
            done.set_result(None)

        threading.Thread(target=lambda: loop.call_soon_threadsafe(cb)).start()
        await done
        alive = v._frontend is not None and v._frontend._thread.is_alive()
        v.stop()
        return alive

    assert asyncio.run(run())


def test_frontend_thread_off_through_settings(monkeypatch):
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, *a, **k: Settings({"FRONTEND_THREAD": "off"})))

    async def run():
        v = VAD(VADConfig(), engine="energy")
        v.update(np.zeros(320, np.float32), None)
        return v._frontend

    assert asyncio.run(run()) is None


def test_update_after_stop_does_not_restart_and_start_reenables():
    async def run():
        v = VAD(VADConfig(), engine="energy", threaded=True)
        v.update(np.zeros(320, np.float32), None)
        v.stop()
        v.update(np.zeros(320, np.float32), None)
        stopped = not v._frontend.running
        v.start()
        v.update(np.zeros(320, np.float32), None)
        restarted = v._frontend.running
        v.stop()
        return stopped, restarted

    assert asyncio.run(run()) == (True, True)


def test_dead_worker_falls_back_to_sync_with_warning(caplog):
    async def run():
        v = VAD(VADConfig(), engine="energy", threaded=True)
        v.update(np.zeros(320, np.float32), None)
        v._frontend._stop.set()
        v._frontend._thread.join()
        for _ in range(3):
            v.update(np.zeros(320, np.float32), None)
        return v._frontend

    with caplog.at_level("WARNING", logger="audio"):
        assert asyncio.run(run()) is None
    assert sum("died" in r.message for r in caplog.records) == 1
