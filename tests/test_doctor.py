"""scripts/doctor.py: rows, exit status, key secrecy, probe isolation; setup.sh failure handling."""
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup.sh"


def _load():
    spec = importlib.util.spec_from_file_location("doctor", ROOT / "scripts" / "doctor.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["doctor"] = mod
    spec.loader.exec_module(mod)
    return mod


doctor = _load()


class _Cfg:
    def __init__(self, **kv):
        self.kv = kv

    def get(self, key, default=""):
        return self.kv.get(key) or default


def _ctx(files=None, globs=None, runs=None, devices=None, **cfg):
    files, globs, runs = files or {}, globs or {}, runs or {}
    devices = devices or {}

    def run(cmd, timeout):
        return runs.get(cmd[0])

    return doctor.Ctx(run=run, read=lambda p: files.get(p), glob=lambda pat: globs.get(pat, []),
                      env={}, devices=lambda kind: devices.get(kind, ([], None)),
                      head=lambda u, t: (True, "HTTP 200"), settings=_Cfg(**cfg), home=Path("/nonexistent"))


def test_doctor_reports_missing_camera_as_degraded_not_error():
    rows = doctor.probe_webcam(_ctx())
    assert [(c.state, c.required) for c in rows] == [("degraded", False)]
    assert "C940e" in rows[0].fix
    ok = doctor.probe_webcam(_ctx(files={"/sys/class/video4linux/video0/name": "HD Pro Webcam C920e\n"},
                                  globs={"/sys/class/video4linux/video*/name":
                                         ["/sys/class/video4linux/video0/name"]}))
    assert ok[0].state == "ok" and "C920e" in ok[0].detail
    full = doctor.report(_ctx(), probes=[("webcam", doctor.probe_webcam)])
    assert doctor.exit_code(full) == 0


def test_doctor_never_prints_key_values(capsys):
    secret = "sk-or-SECRET-VALUE-123"
    ctx = _ctx(OPENROUTER_API_KEY=secret, MAPBOX_ACCESS_TOKEN="pk.SECRET-MAP", EXA_API_KEY="exa-SECRET")
    checks = doctor.report(ctx, probes=[("keys", doctor.probe_keys)])
    blob = doctor.render(checks) + json.dumps([c.__dict__ for c in checks])
    assert "SECRET" not in blob
    by = {c.name: c for c in checks}
    assert by["key OPENROUTER_API_KEY"].state == "ok" and by["key TYPESAFE_API_KEY"].state == "degraded"


def test_missing_required_key_gives_exit_1_and_optional_does_not():
    checks = doctor.report(_ctx(MAPBOX_ACCESS_TOKEN="x"), probes=[("keys", doctor.probe_keys)])
    assert doctor.exit_code(checks) == 1
    checks = doctor.report(_ctx(MAPBOX_ACCESS_TOKEN="x", OPENROUTER_API_KEY="y"), probes=[("keys", doctor.probe_keys)])
    assert doctor.exit_code(checks) == 0


def test_probe_crash_and_timeout_cannot_break_the_report():
    def boom(ctx):
        raise RuntimeError("x")

    def hang(ctx):
        time.sleep(5)
        return []

    out = doctor.report(_ctx(), probes=[("boom", boom), ("hang", hang), ("webcam", doctor.probe_webcam)],
                        timeout=0.2)
    assert [c.state for c in out] == ["degraded", "degraded", "degraded"]
    assert "RuntimeError" in out[0].detail and "timed out" in out[1].detail


def test_echo_path_by_alsa_card():
    assert "hardware AEC" in doctor.probe_echo(_ctx(files={"/proc/asound/cards": " 2 [XVF3800 ]: USB-Audio"}))[0].detail
    r = doctor.probe_echo(_ctx(files={"/proc/asound/cards": " 0 [wm8960soundcard]: simple-card"}))[0]
    assert "WM8960" in r.detail and "software AEC" in r.detail


def test_temperature_na_without_vcgencmd_and_throttle_flagged():
    assert "n/a" in doctor.probe_temp(_ctx())[0].detail
    r = doctor.probe_temp(_ctx(runs={"vcgencmd": "temp=78.4'C"}))[0]
    assert r.state == "degraded"


def test_f455_firmware_needs_target(monkeypatch):
    cams = {"/dev/v4l/by-id/usb-Intel_Intel_F45*-video-index0": ["/dev/v4l/by-id/x"]}
    monkeypatch.setattr(doctor, "read_f455_modules", lambda ctx: {"OPFW": "1.6.1.1007"})
    r = doctor.probe_f455(_ctx(globs=cams))[0]
    assert r.state == "degraded" and "F455 firmware" in r.fix
    monkeypatch.setattr(doctor, "read_f455_modules", lambda ctx: {"OPFW": doctor.F455_TARGET_OPFW})
    assert doctor.probe_f455(_ctx(globs=cams))[0].state == "ok"
    assert doctor.probe_f455(_ctx())[0].state == "degraded"


def test_cli_json_and_table_run_muted():
    env = {**os.environ, "NOVA_AUDIO_MUTE": "1"}
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "doctor.py"), "--json"], capture_output=True,
                       text=True, env=env, cwd=ROOT, timeout=120, check=False)
    assert r.returncode in (0, 1), r.stderr
    data = json.loads(r.stdout)
    assert data["ok"] == (r.returncode == 0)
    names = {c["name"] for c in data["checks"]}
    assert {"mic", "speaker", "network", "disk", "temperature", "model Phonon"} <= names


# ── setup.sh failure handling ────────────────────────────────────────────────────────────────────────────

def _stub(dirpath: Path, name: str, body: str) -> None:
    p = dirpath / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


def _setup_env(tmp_path, bin_dir=None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    path = f"{bin_dir}:{os.environ['PATH']}" if bin_dir else os.environ["PATH"]
    return {**os.environ, "HOME": str(home), "SETUP_PREFIX": str(tmp_path / "prefix"), "PATH": path}


def test_failed_download_makes_setup_exit_1(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "curl", "exit 22\n")
    _stub(bin_dir, "git", "exit 128\n")
    r = subprocess.run(["bash", str(SETUP), "--yes", "--no-apt"], capture_output=True, text=True, check=False,
                       env={**_setup_env(tmp_path, bin_dir), "SETUP_STEPS": "models"}, cwd=tmp_path, timeout=120)
    assert r.returncode == 1
    assert "download failed" in r.stderr
    assert "Setup finished with problems" in r.stdout
    assert "Setup complete" not in r.stdout


def test_doctor_failure_in_setup_is_reported_as_problem(tmp_path):
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SETUP, root / "setup.sh")
    (root / "models.lock").write_text("")
    (root / "scripts" / "doctor.py").write_text("import sys\nsys.exit(1)\n")
    r = subprocess.run(["bash", str(root / "setup.sh"), "--yes"], capture_output=True, text=True, check=False,
                       env={**_setup_env(tmp_path), "SETUP_STEPS": "check"}, cwd=tmp_path, timeout=60)
    assert r.returncode == 1
    assert "Setup finished with problems" in r.stdout and "Setup complete" not in r.stdout


def test_env_fill_leaves_no_trailing_space_and_keeps_comment(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(SETUP, root / "setup.sh")
    (root / "models.lock").write_text("")
    (root / ".env").write_text("A=1\nSILERO_MODEL=                       # blank = default\nSMART_TURN_MODEL=\n")
    snippet = f'source <(sed -n "/^fill_env_blank()/,/^}}/p" {root}/setup.sh); ROOT={root}; ' \
              'fill_env_blank SILERO_MODEL /m/s.onnx; fill_env_blank SMART_TURN_MODEL /m/t.onnx'
    subprocess.run(["bash", "-c", snippet], check=True, cwd=tmp_path)
    lines = (root / ".env").read_text().splitlines()
    assert lines[1] == "SILERO_MODEL=/m/s.onnx  # blank = default"
    assert lines[2] == "SMART_TURN_MODEL=/m/t.onnx"
    assert all(ln == ln.rstrip() for ln in lines)


def test_models_lock_pins_smart_turn_to_a_commit():
    text = (ROOT / "models.lock").read_text()
    assert "resolve/main" not in text
    assert "resolve/f766f81d3cfdf7737ac64aad813d91bbfd56bf93/" in text


@pytest.mark.parametrize("name", ["lock_path"])
def test_lock_path_prefers_newest_snapshot(tmp_path, name):
    hub = tmp_path / "hub" / "models--x" / "snapshots"
    for i, snap in enumerate(["old", "new"]):
        (hub / snap).mkdir(parents=True)
        f = hub / snap / "model.safetensors"
        f.write_text(snap)
        os.utime(f, (1000 + i * 100, 1000 + i * 100))
    snippet = f'source <(sed -n "/^lock_path()/,/^}}/p" {SETUP}); HUB={tmp_path}/hub; MODELS=/none; ' \
              'lock_path "@hf/models--x/snapshots/*/model.safetensors"'
    out = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, check=True).stdout
    assert out.endswith("/new/model.safetensors")


WM = (0, "wm8960-soundcard: bcm2835-i2s-wm8960-hifi wm8960-hifi-0 (hw:0,0)")
XVF = (1, "XVF3800 4-Mic Array: USB Audio (hw:2,0)")
PW = [(2, "pipewire"), (3, "default")]


def test_mic_wm8960_unset_uses_pipewire_not_first_device():
    devs = {"input": ([WM, *PW], "default")}
    r = doctor.probe_mic(_ctx(devices=devs))[0]
    assert r.state == "ok" and r.detail.startswith("pipewire") and "shared via PipeWire" in r.detail


def test_mic_wm8960_without_pipewire_reports_default_raw_hw():
    devs = {"input": ([XVF, WM], WM[1])}
    r = doctor.probe_mic(_ctx(devices=devs))[0]
    assert r.state == "degraded" and "wm8960" in r.detail and "raw hw" in r.detail


def test_mic_named_prefers_shared_entry_and_missing_device_is_missing():
    devs = {"input": ([XVF, (4, "XVF3800 Mono (pulse)")], None)}
    assert "pulse" in doctor.probe_mic(_ctx(devices=devs, AUDIO_INPUT_DEVICE="xvf3800"))[0].detail
    assert doctor.probe_mic(_ctx())[0].state == "missing"
    assert doctor.probe_mic(_ctx(devices=devs, AUDIO_INPUT_DEVICE="nope"))[0].state == "degraded"


def test_speaker_probe():
    assert doctor.probe_speaker(_ctx())[0].state == "missing"
    devs = {"output": ([WM, *PW], "default")}
    assert doctor.probe_speaker(_ctx(devices=devs))[0].detail == "default"
    assert doctor.probe_speaker(_ctx(devices=devs, AUDIO_OUTPUT_DEVICE="usb"))[0].state == "degraded"


def test_echo_follows_the_resolved_mic_when_both_cards_exist():
    cards = {"/proc/asound/cards": " 0 [wm8960soundcard]: x\n 2 [XVF3800]: y"}
    on_wm = _ctx(files=cards, devices={"input": ([WM, XVF], WM[1])})
    assert "software AEC" in doctor.probe_echo(on_wm)[0].detail
    on_xvf = _ctx(files=cards, devices={"input": ([WM, XVF], XVF[1])})
    assert "hardware AEC" in doctor.probe_echo(on_xvf)[0].detail
    via_pw = _ctx(files=cards, devices={"input": ([WM, XVF, *PW], "default")},
                  runs={"pactl": "alsa_input.usb-XVF3800_4-Mic.mono-fallback"})
    assert "hardware AEC" in doctor.probe_echo(via_pw)[0].detail


def test_gps_without_device_and_with_gpsd_fix():
    r = doctor.probe_gps(_ctx())[0]
    assert r.state == "degraded" and "simulated" in r.detail
    port = {"/dev/serial/by-id/usb-1a86_*": ["/dev/serial/by-id/usb-1a86_x"]}
    assert "gpsd not answering" in doctor.probe_gps(_ctx(globs=port))[0].detail
    tpv = '{"class":"TPV","device":"/dev/x","mode":3,"lat":1}'
    assert doctor.probe_gps(_ctx(globs=port, runs={"gpspipe": tpv}))[0].state == "ok"
    nofix = '{"class":"TPV","mode":1}'
    assert "no fix" in doctor.probe_gps(_ctx(globs=port, runs={"gpspipe": nofix}))[0].detail


def test_malformed_settings_give_config_row(monkeypatch):
    from cabin import config

    def bad(*a, **k):
        raise ValueError("bad env")

    monkeypatch.setattr(config.Settings, "load", classmethod(bad))
    out = doctor.report()
    assert out[0].name == "config" and out[0].state == "missing" and doctor.exit_code(out) == 1


def test_probes_run_concurrently():
    def slow(ctx):
        time.sleep(0.5)
        return [doctor.Check("s", "ok", "")]

    t0 = time.monotonic()
    out = doctor.report(_ctx(), probes=[(f"p{i}", slow) for i in range(6)])
    assert len(out) == 6 and time.monotonic() - t0 < 2.0


def test_env_fill_edits_only_the_first_blank_line(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    (root / ".env").write_text("SILERO_MODEL=/set\nSILERO_MODEL=\n")
    snippet = f'source <(sed -n "/^fill_env_blank()/,/^}}/p" {SETUP}); ROOT={root}; fill_env_blank SILERO_MODEL /m'
    subprocess.run(["bash", "-c", snippet], check=True)
    assert (root / ".env").read_text().splitlines() == ["SILERO_MODEL=/set", "SILERO_MODEL=/m"]


def test_lock_path_handles_spaces(tmp_path):
    d = tmp_path / "my hub" / "snap"
    d.mkdir(parents=True)
    (d / "m.bin").write_text("x")
    snippet = f'source <(sed -n "/^lock_path()/,/^}}/p" {SETUP}); HUB="{tmp_path}/my hub"; MODELS=/none; ' \
              'lock_path "@hf/snap/m.bin"'
    out = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, check=True).stdout
    assert out.endswith("my hub/snap/m.bin")


def test_mic_named_raw_hw_falls_back_to_pipewire_like_the_app():
    devs = {"input": ([XVF, *PW], "default")}
    r = doctor.probe_mic(_ctx(devices=devs, AUDIO_INPUT_DEVICE="xvf3800"))[0]
    assert r.state == "ok" and r.detail.startswith("pipewire")
