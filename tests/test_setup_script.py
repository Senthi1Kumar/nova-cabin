"""setup.sh: syntax, dry-run step list, lock file shape, and that nothing is executed in dry-run."""
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup.sh"

STEPS = ["preflight", "apt", "uv", "venv", "requirements", "driveos", "driveauth", "models", "audio", "map",
         "env", "desktop", "check"]


def _dry(tmp_path, *args):
    env = {**os.environ, "SETUP_DRY_RUN": "1", "SETUP_PREFIX": str(tmp_path / "prefix"),
           "HOME": str(tmp_path / "home")}
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run(["bash", str(SETUP), "--yes", *args], check=False, capture_output=True, text=True, env=env,
                          cwd=tmp_path, timeout=60)


def test_bash_syntax():
    r = subprocess.run(["bash", "-n", str(SETUP)], check=False, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_dry_run_lists_every_step_in_order(tmp_path):
    r = _dry(tmp_path)
    assert r.returncode == 0, r.stderr
    found = re.findall(r"^\[dry-run\] STEP (\w+):", r.stdout, re.MULTILINE)
    assert found == STEPS


def test_dry_run_executes_nothing(tmp_path):
    r = _dry(tmp_path)
    assert r.returncode == 0
    prefix = tmp_path / "prefix"
    assert not (prefix / "venv").exists()
    assert not (tmp_path / "home" / ".local").exists()
    assert not (tmp_path / "home" / ".cache" / "nova-cabin" / "drive-os-src").exists()


def test_no_apt_skips_apt_commands(tmp_path):
    r = _dry(tmp_path, "--no-apt")
    assert "apt-get install" not in r.stdout
    assert "libportaudio2" not in r.stdout
    r = _dry(tmp_path)
    assert "libportaudio2 espeak-ng gpsd gpsd-clients libspeexdsp1 python3-venv" in r.stdout


def test_dry_run_installs_pinned_driveos_and_wheel(tmp_path):
    out = _dry(tmp_path).stdout
    assert "requirements-pi.txt" in out
    assert "driveauth_edge-" in out


def test_check_without_doctor_is_tolerated(tmp_path):
    env = {**os.environ, "SETUP_DRY_RUN": "1", "SETUP_PREFIX": str(tmp_path / "p"), "HOME": str(tmp_path)}
    r = subprocess.run(["bash", str(SETUP), "--check"], check=False, capture_output=True, text=True, env=env,
                       cwd=tmp_path,
                       timeout=60)
    assert r.returncode in (0, 1)
    assert "doctor" in r.stdout + r.stderr


def test_models_lock_shape():
    rows = [ln for ln in (ROOT / "models.lock").read_text().splitlines() if ln and not ln.startswith("#")]
    assert len(rows) >= 8
    for ln in rows:
        name, path, sha, source = ln.split("|")
        assert name and path
        assert sha == "-" or re.fullmatch(r"[0-9a-f]{64}", sha)
        assert source.split(":", 1)[0] in ("url", "cmd", "build")
    names = {ln.split("|")[0] for ln in rows}
    assert {"silero_vad.onnx", "smart-turn-v3.2-cpu.onnx", "parakeet-tdt-ctc-110m", "phonon-31m-en"} <= names


def test_vendor_wheel_and_pi_requirements_present():
    assert list((ROOT / "packaging" / "vendor").glob("driveauth_edge-*.whl"))
    text = (ROOT / "requirements-pi.txt").read_text()
    assert "drive-os@v1.2.0" in text and "voice,biometrics" in text
