import asyncio
import json
import sys
import threading
import time
import types
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import export_logs

from cabin.telemetry.sampler import Sampler


def _absent(cmd):
    return None


def test_sampler_handles_missing_vcgencmd_and_hailo(tmp_path):
    s = Sampler(interval_s=1, run=_absent, sys_root=tmp_path)
    rec = s.sample()
    assert rec["temp_c"] is None and rec["arm_mhz"] is None
    assert rec["throttled"] is None and rec["npu_util"] is None and rec["fps"] is None
    assert rec["npu"] == "absent"
    assert isinstance(rec["cpu_per_core"], list)
    assert rec["rss_mb"] is None or rec["rss_mb"] > 0
    assert isinstance(rec["threads_busy"], int)


def test_sampler_reads_sysfs_vcgencmd_npu_fps_and_emits_diagnostics(tmp_path):
    (tmp_path / "class/thermal/thermal_zone0").mkdir(parents=True)
    (tmp_path / "class/thermal/thermal_zone0/temp").write_text("61500\n")
    (tmp_path / "devices/system/cpu/cpu0/cpufreq").mkdir(parents=True)
    (tmp_path / "devices/system/cpu/cpu0/cpufreq/scaling_cur_freq").write_text("2400000\n")
    out = {"get_throttled": "throttled=0x50005\n"}
    events, perf = [], []

    class Tel:
        def perf(self, **f):
            perf.append(f)

        def status(self):
            return {"enabled": True, "dropped": 0}

    def run(cmd):
        if cmd[0] == "vcgencmd":
            return out.get(cmd[1])
        return "Device Architecture: HAILO10H\n" if cmd[:2] == ["hailortcli", "fw-control"] else None

    eng = types.SimpleNamespace(ui_fps=58.0, ui_fps_ts=time.monotonic())
    s = Sampler(run=run, telemetry=Tel(), engine=eng, emit=lambda k, v: events.append((k, v)), sys_root=tmp_path)
    s.tick()
    assert perf[0]["temp_c"] == 61.5 and perf[0]["arm_mhz"] == 2400.0 and perf[0]["throttled"] is True
    assert perf[0]["npu"] == "present" and perf[0]["fps"] == 58.0
    eng.ui_fps_ts -= 10
    assert s.sample()["fps"] is None
    key, diag = events[0]
    assert key == "diagnostics" and diag["temp_c"] == 61.5 and diag["telemetry"]["enabled"]


def _engine(tmp_path, **raw):
    from cabin import telemetry
    from cabin.config import Settings
    telemetry.reset()
    cfg = Settings.load(None)
    cfg.raw.update({"NOVA_CABIN_DATA": str(tmp_path), "TELEMETRY_SAMPLE_S": "0.05", **raw})
    return types.SimpleNamespace(cfg=cfg, closers=[], emit=lambda k, v: None)


def test_plugin_starts_and_stops_sampler(tmp_path):
    from cabin.telemetry import plugin
    eng = _engine(tmp_path)
    plugin.install(eng)
    try:
        assert len(eng.closers) == 1
        time.sleep(0.4)
        asyncio.run(eng.closers[0]())
        assert not [t for t in threading.enumerate() if t.name == "telemetry-sampler" and t.is_alive()]
    finally:
        from cabin import telemetry
        telemetry.reset()


def test_sampler_flag_off_starts_no_thread(tmp_path):
    from cabin import telemetry
    from cabin.telemetry import plugin
    eng = _engine(tmp_path, TELEMETRY_SAMPLER="off")
    plugin.install(eng)
    try:
        assert not [t for t in threading.enumerate() if t.name == "telemetry-sampler" and t.is_alive()]
    finally:
        asyncio.run(eng.closers[0]())
        telemetry.reset()


def _write(root, stream, rows):
    (root / f"{stream}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_export_zip_contains_four_logs(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    _write(logs, "app", [{"kind": "log", "ts": 100.0, "msg": "boot"}])
    _write(logs, "conversation", [{"kind": "text", "ts": 101.0, "turn_id": "t1", "role": "user", "text": "hi",
                                   "api_key": "sk-or-abcdefgh12345678"},
                                  {"kind": "text", "ts": 200.0, "turn_id": "t2", "role": "user", "text": "other"}])
    _write(logs, "tools", [{"kind": "tool", "ts": 101.5, "turn_id": "t1", "name": "x"}])
    _write(logs, "perf", [{"kind": "turn", "ts": 101.0, "turn_id": "t1", "total_ms": 900, "stt_ms": 100},
                          {"kind": "turn", "ts": 200.0, "turn_id": "t2", "total_ms": 500}])
    out = tmp_path / "r.zip"
    counts = export_logs.export(out, root=logs, session="t1")
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert {"app.jsonl", "conversation.jsonl", "tools.jsonl", "perf.jsonl", "perf_summary.csv"} <= names
        conv = z.read("conversation.jsonl").decode()
        assert "t2" not in conv and "sk-or-abcdefgh" not in conv
        assert "t1" in z.read("perf_summary.csv").decode()
    assert counts["tools"] == 1 and counts["app"] == 1
    assert export_logs.export(tmp_path / "l.zip", root=logs, last=True)["conversation"] == 1
