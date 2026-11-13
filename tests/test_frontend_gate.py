"""Driver zone: DoA on the XVF3800, near-field level on a single mic."""
import struct

import numpy as np

from cabin.frontend.gate import DriverGate, DriverGateStage, XvfDoa
from cabin.frontend.worker import Context


def _tone(db, n=320):
    return np.full(n, 10 ** (db / 20), np.float32)


def test_gate_rejects_out_of_zone_doa():
    g = DriverGate(bearing_deg=0, zone_deg=40)
    assert g.allow(_tone(-30), 120.0, -50.0) is False
    assert g.allow(_tone(-30), 20.0, -50.0) is True
    assert g.allow(_tone(-30), 350.0, -50.0) is True        # wraps around north
    assert g.allow(_tone(-30), 300.0, -50.0) is False


def test_gate_bearing_offset():
    g = DriverGate(bearing_deg=90, zone_deg=40)
    assert g.allow(_tone(-30), 100.0, -50.0) is True
    assert g.allow(_tone(-30), 0.0, -50.0) is False


def test_gate_near_field_without_doa():
    g = DriverGate(near_field_db=18)
    assert g.allow(_tone(-30), None, -50.0) is True         # 20 dB over the floor
    assert g.allow(_tone(-40), None, -50.0) is False        # 10 dB over the floor


class _Doa:
    present = True

    def __init__(self, deg):
        self.deg = deg

    def read(self):
        return self.deg


def _ctx():
    return Context(None, 0.0, {})


def test_stage_drops_off_axis_speech_and_passes_driver():
    doa = _Doa(150.0)
    st = DriverGateStage(DriverGate(), doa=doa, mode="doa", hold_frames=0)
    assert st.process(_tone(-30), _ctx()) is None
    doa.deg = 10.0
    assert st.process(_tone(-30), _ctx()) is not None


def test_stage_holds_open_after_driver_frame():
    doa = _Doa(10.0)
    st = DriverGateStage(DriverGate(), doa=doa, mode="doa", hold_frames=5)
    assert st.process(_tone(-30), _ctx()) is not None
    doa.deg = None
    assert all(st.process(_tone(-60), _ctx()) is not None for _ in range(5))
    assert st.process(_tone(-60), _ctx()) is None


def test_stage_passes_everything_while_a_turn_runs():
    doa = _Doa(150.0)
    st = DriverGateStage(DriverGate(), doa=doa, mode="doa", hold_frames=0, in_speech=lambda: True)
    assert st.process(_tone(-30), _ctx()) is not None


def test_stage_near_field_learns_floor_then_gates():
    st = DriverGateStage(DriverGate(near_field_db=18), mode="near", hold_frames=0, warmup_frames=10)
    for _ in range(30):
        st.process(_tone(-60), _ctx())
    assert st.process(_tone(-60), _ctx()) is None
    assert st.process(_tone(-30), _ctx()) is not None


def test_stage_off_passes_all():
    st = DriverGateStage(DriverGate(), mode="off")
    assert st.process(_tone(-80), _ctx()) is not None


class _Dev:
    def __init__(self, az):
        self.az = az
        self.timeouts = []

    def ctrl_transfer(self, rt, req, value, index, length, timeout):
        self.timeouts.append(timeout)
        return bytes([0]) + struct.pack("<2f", self.az, float("nan"))


def test_xvf_doa_reads_degrees_with_short_timeout():
    dev = _Dev(np.pi / 2)
    d = XvfDoa(find=lambda: dev)
    assert d.open()
    d.poll_once()
    assert abs(d.read() - 90.0) < 0.01
    assert set(dev.timeouts) == {50}


def test_xvf_doa_nan_means_none_and_absent_is_none():
    d = XvfDoa(find=lambda: _Dev(float("nan")))
    d.open()
    d.poll_once()
    assert d.read() is None
    absent = XvfDoa(find=lambda: None)
    assert absent.open() is False and absent.read() is None and absent.present is False


def test_replayed_frames_pass_the_driver_gate():
    from cabin.frontend.gate import _GatedVad

    class Vad:
        def __init__(self):
            self.seen = 0

        def process(self, frame, ctx):
            self.seen += 1
            return frame

    doa = _Doa(150.0)
    vad = Vad()
    proxy = _GatedVad(DriverGateStage(DriverGate(), doa=doa, mode="doa", hold_frames=0), vad)
    assert proxy.process(_tone(-30), _ctx()) is None and vad.seen == 0
    doa.deg = 5.0
    assert proxy.process(_tone(-30), _ctx()) is not None and vad.seen == 1


def test_worker_installs_gate_and_wake_stage_around_the_vad():
    from cabin.audio import VadStage
    from cabin.config import Settings
    from cabin.frontend import FrontEnd
    from cabin.frontend.gate import WakeStage
    fe = FrontEnd(Settings({"DRIVER_GATE": "near_field", "WAKE_WORD": "hey nova"}), lambda e: None,
                  stages=[VadStage(lambda f, p: (None, 0.0))])
    names = [type(s).__name__ for s in fe.stages]
    assert names.index("DriverGateStage") < names.index("VadStage") < names.index("WakeStage")
    assert isinstance(fe.stages[-1], WakeStage)
    assert fe.stats["driver_gate"] == "near" or fe.stats["driver_gate"] == "pending"


def test_defaults_leave_the_chain_untouched():
    from cabin.audio import VadStage
    from cabin.config import Settings
    from cabin.frontend import FrontEnd
    fe = FrontEnd(Settings({}), lambda e: None, stages=[VadStage(lambda f, p: (None, 0.0))])
    assert not any(type(s).__name__ in ("DriverGateStage", "WakeStage") for s in fe.stages)
