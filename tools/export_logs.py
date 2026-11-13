"""Zip a session's telemetry logs for a bug report.

    python tools/export_logs.py --last --out report.zip
    python tools/export_logs.py --since 1h --out report.zip
    python tools/export_logs.py --session 101530-3 --out report.zip

Selects rows of app / conversation / tools / perf by session (turn_id) or time window, redacts them
again, and adds ``perf_summary.csv`` (one row per turn).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cabin.telemetry.redact import redact

STREAMS = ("app", "conversation", "tools", "perf")
SUMMARY_COLS = ("turn_id", "ts", "stt_ms", "first_token_ms", "tools_ms", "tts_first_ms", "first_audio_ms",
                "tail_ms", "total_ms", "ttfa_ms", "model", "tokens_in", "tokens_out", "cost")


def log_dir() -> Path:
    base = os.environ.get("NOVA_CABIN_DATA") or str(Path.home() / ".local/share/nova-cabin")
    return Path(base) / "logs"


def parse_since(text: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd])", text.strip().lower())
    if not m:
        raise ValueError(f"--since expects e.g. 30m, 1h, 2d: {text!r}")
    return float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def read_stream(root: Path, stream: str) -> list[dict]:
    files = sorted(root.glob(f"{stream}.*.jsonl"), key=lambda p: p.stat().st_mtime)
    live = root / f"{stream}.jsonl"
    rows: list[dict] = []
    for p in [*files, *([live] if live.exists() else [])]:
        for line in p.read_text(errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                rows.append(rec)
    return rows


def last_turn_id(rows: dict[str, list[dict]]) -> str | None:
    turns = [r for r in rows.get("perf", []) if r.get("kind") == "turn" and r.get("turn_id")]
    return max(turns, key=lambda r: r.get("ts") or 0)["turn_id"] if turns else None


def select(rows: list[dict], session: str | None, since_ts: float | None, span: tuple[float, float] | None) -> list[dict]:
    out = []
    for r in rows:
        ts = r.get("ts") or 0
        if session is not None:
            if r.get("turn_id") == session or (span and span[0] <= ts <= span[1] and r.get("turn_id") in (None, session)):
                out.append(r)
        elif since_ts is None or ts >= since_ts:
            out.append(r)
    return out


def export(out: Path, root: Path | None = None, session: str | None = None, last: bool = False,
           since: str | None = None) -> dict[str, int]:
    root = root or log_dir()
    data = {s: read_stream(root, s) for s in STREAMS}
    since_ts = time.time() - parse_since(since) if since else None
    span = None
    if last:
        session = last_turn_id(data)
    if session is not None:
        turn = next((r for r in data["perf"] if r.get("kind") == "turn" and r.get("turn_id") == session), None)
        if turn and turn.get("ts"):
            span = (turn["ts"] - 5, turn["ts"] + (turn.get("total_ms") or 0) / 1000.0 + 5)
    counts: dict[str, int] = {}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        picked = {s: select(data[s], session, since_ts, span) for s in STREAMS}
        for s in STREAMS:
            body = "".join(json.dumps(redact(r), default=str) + "\n" for r in picked[s])
            z.writestr(f"{s}.jsonl", body)
            counts[s] = len(picked[s])
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(SUMMARY_COLS)
        for r in picked["perf"]:
            if r.get("kind") == "turn":
                w.writerow([r.get(c) for c in SUMMARY_COLS])
        z.writestr("perf_summary.csv", buf.getvalue())
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--session", help="turn id")
    g.add_argument("--last", action="store_true", help="the most recent turn")
    g.add_argument("--since", help="time window such as 30m, 1h, 2d")
    ap.add_argument("--out", required=True, help="zip path")
    ap.add_argument("--logs", help="log directory (default $NOVA_CABIN_DATA/logs)")
    a = ap.parse_args(argv)
    counts = export(Path(a.out), Path(a.logs) if a.logs else None, a.session, a.last, a.since)
    print(a.out, counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
