import json
import sqlite3

import pytest

from cabin.telemetry.redact import redact
from cabin.telemetry.sink import Sink

LEAKS = [
    ("OPENROUTER_API_KEY=sk-or-v1-abcdef1234567890abcdef1234567890", "abcdef1234567890"),
    ("token sk-or-v1-0123456789abcdef0123456789abcdef used", "0123456789abcdef"),
    ("map pk.eyJ1IjoiZm9vIiwiYSI6ImJhciJ9.sig_part-xyz", "eyJ1IjoiZm9v"),
    ("EXA_API_KEY: exa-secret-value-1234", "exa-secret-value-1234"),
    ("TYPESAFE_TOKEN=ts_live_9f8e7d6c5b4a", "ts_live_9f8e7d6c5b4a"),
    ("card 4111 1111 1111 1111 ok", "4111 1111 1111 1111"),
    ("card 4111-1111-1111-1111 ok", "4111-1111-1111-1111"),
    ("pan 378282246310005", "378282246310005"),
    ("your code is 482913", "482913"),
    ("482913 is the OTP", "482913"),
    ("PIN: 7731", "7731"),
    ("the otp, uh, 4 8 2 9 1 7", "4 8 2 9 1 7"),
    ("deliver to 221 Baker Street please", "221 Baker Street"),
    ("flat at 12 MG Road, Bengaluru", "12 MG Road"),
    ("it is 14 Gandhi Salai", "14 Gandhi Salai"),
    ("42 Anna Nagar", "42 Anna Nagar"),
    ("17 5th Cross", "17 5th Cross"),
    ("no 8 Indiranagar 2nd Main", "8 Indiranagar 2nd Main"),
    ("7 HSR Layout", "7 HSR Layout"),
    ("9 Oak St", "9 Oak St"),
]


@pytest.mark.parametrize("text,secret", LEAKS)
def test_redact_keys_pans_otps_addresses(text, secret):
    out = redact(text)
    assert secret not in out
    assert "[redacted" in out


def test_key_names_redacted_in_dicts():
    out = redact({"OPENROUTER_API_KEY": "x1", "auth": {"MAPBOX_TOKEN": "x2", "api_key": "x3"}, "tokens_in": 12})
    flat = json.dumps(out)
    assert "x1" not in flat and "x2" not in flat and "x3" not in flat
    assert out["tokens_in"] == 12


def test_biometric_scores_redacted_in_context():
    out = redact({"biometric": {"score": 0.91, "llr": 3.2, "cosine": 0.8, "result": "pass"}})
    assert out["biometric"]["result"] == "pass"
    for k in ("score", "llr", "cosine"):
        assert out["biometric"][k] != pytest.approx(0.91) and "redacted" in str(out["biometric"][k])
    out = redact({"score": 0.77, "kind": "voice_verify"})
    assert "redacted" in str(out["score"])
    assert redact({"score": 0.77, "kind": "search"})["score"] == 0.77
    assert "redacted" in str(redact({"score": 0.5}, context="verify_speaker")["score"])


def test_gps_and_places_kept():
    v = {"lat": 37.7793, "lon": -122.4193, "place": "Reno, NV", "text": "navigate to Reno in 12 minutes"}
    assert redact(v) == v
    assert redact("at 37.7793, -122.4193 near San Francisco") == "at 37.7793, -122.4193 near San Francisco"


def test_registered_secret_values_scrubbed(tmp_path):
    from cabin.telemetry.redact import add_secret, clear_secrets
    clear_secrets()
    add_secret("hunter2hunter2")
    try:
        assert "hunter2hunter2" not in redact("password hunter2hunter2 here")
    finally:
        clear_secrets()


def _sink(tmp_path, **kw):
    return Sink(tmp_path, **kw)


def test_sink_redacts_on_write(tmp_path):
    s = _sink(tmp_path)
    s.write("conversation", {"text": "my code is 123456 and card 4111 1111 1111 1111"})
    s.flush()
    s.close()
    line = (tmp_path / "conversation.jsonl").read_text()
    assert "123456" not in line and "4111" not in line


def test_rotation(tmp_path):
    now = [1_000_000.0]
    s = _sink(tmp_path, max_bytes=400, max_days=14, clock=lambda: now[0])
    for i in range(20):
        s.write("app", {"msg": f"line {i} " + "x" * 40})
    s.flush()
    rotated = sorted(p.name for p in tmp_path.glob("app.*.jsonl"))
    assert rotated, "size rotation produced no archive"
    assert (tmp_path / "app.jsonl").stat().st_size <= 400 + 120
    # age rotation and pruning
    now[0] += 15 * 86400
    import os
    for p in tmp_path.glob("app.*.jsonl"):
        os.utime(p, (1_000_000.0, 1_000_000.0))
    s.write("app", {"msg": "after"})
    s.flush()
    s.close()
    assert not list(tmp_path.glob("app.*.jsonl")) or all(
        "after" not in p.read_text() for p in tmp_path.glob("app.*.jsonl"))
    assert "after" in (tmp_path / "app.jsonl").read_text()
    assert len(list(tmp_path.glob("app.*.jsonl"))) <= 1


def test_queue_overflow_drops_and_counts(tmp_path):
    import threading
    gate = threading.Event()
    s = _sink(tmp_path, queue_size=5, _hold=gate)
    for i in range(50):
        s.write("app", {"i": i})
    assert s.dropped >= 40
    gate.set()
    s.flush()
    s.close()


def test_sqlite_mirror(tmp_path):
    s = _sink(tmp_path)
    s.write("perf", {"kind": "turn", "turn_id": "t1", "total_ms": 900.0, "stt_ms": 100.0})
    s.write("tools", {"kind": "tool", "turn_id": "t1", "name": "x", "agent": "a", "duration_ms": 5.0})
    s.write("conversation", {"kind": "llm", "turn_id": "t1", "model": "m", "tokens_in": 3, "tokens_out": 4})
    s.flush()
    s.close()
    db = sqlite3.connect(tmp_path / "telemetry.db")
    assert db.execute("select total_ms from turns").fetchone() == (900.0,)
    assert db.execute("select name from tools").fetchone() == ("x",)
    assert db.execute("select tokens_out from llm").fetchone() == (4,)


FIX_LEAKS = [
    ("GET /x?token=abcdef123456&y=1", "abcdef123456"),
    ("url api_key=abcdef12345", "abcdef12345"),
    ('{"access_token": "tk.abcdefghijk"}', "abcdefghijk"),
    ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
    ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
    ("tk.eyJhbGciOiJ.sigpart", "eyJhbGciOiJ"),
    ("voice id verify score=0.93 ok", "0.93"),
    ("biometric llr: -2.5", "2.5"),
]


@pytest.mark.parametrize("text,secret", FIX_LEAKS)
def test_redact_key_forms_and_text_scores(text, secret):
    assert secret not in redact(text)


def test_non_string_objects_scrubbed(tmp_path):
    out = redact({"e": RuntimeError("failed access_token=zzzzzz1234 pan 4111 1111 1111 1111 code 556677"),
                  "b": b"api_key=bytesecret99", "s": {"password=hunter2hunter2"}})
    flat = json.dumps(out)
    for leak in ("zzzzzz1234", "4111", "556677", "bytesecret99", "hunter2hunter2"):
        assert leak not in flat
    s = _sink(tmp_path)
    s.write("app", {"o": object(), "e": RuntimeError("code 112233")})
    s.flush()
    s.close()
    assert "112233" not in (tmp_path / "app.jsonl").read_text()


def test_close_is_bounded_and_idempotent(tmp_path):
    import threading
    import time
    gate = threading.Event()
    s = _sink(tmp_path, queue_size=2, _hold=gate)
    for i in range(10):
        s.write("app", {"i": i})
    t0 = time.monotonic()
    s.close()
    s.close()
    assert time.monotonic() - t0 < 3
    gate.set()
