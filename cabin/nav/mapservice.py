"""Shared-memory frames and a Unix-socket control line for nova-map-service.

The header is six little-endian uint32s: seq, width, height, stride, flags, reserved.
Pixels follow as tightly packed RGBA. ``flags`` bit 0 is set while the service is paused.
"""
from __future__ import annotations

import asyncio
import json
import logging
import mmap
import os
import socket
import struct
import time
from pathlib import Path

HEADER = struct.Struct("<6I")
HEADER_SIZE = HEADER.size
READ_RETRIES = 4
log = logging.getLogger("nav.mapservice")


class MapServiceClient:
    def __init__(self, shm_path: str | Path, sock_path: str | Path):
        self.shm_path = Path(shm_path)
        self.sock_path = Path(sock_path)
        self._file = None
        self._mm = None
        self._sock: socket.socket | None = None

    def connect(self, timeout_s: float = 2.0) -> None:
        deadline = time.monotonic() + timeout_s
        sock = None
        while True:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(str(self.sock_path))
                break
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                if sock is not None:
                    sock.close()
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        self._sock = sock
        fd = os.open(self.shm_path, os.O_RDONLY)
        self._file = os.fdopen(fd, "rb")
        size = self.shm_path.stat().st_size
        if size < HEADER_SIZE:
            raise RuntimeError("map frame is not ready")
        self._mm = mmap.mmap(self._file.fileno(), size, access=mmap.ACCESS_READ)

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        if self._mm is not None:
            self._mm.close()
            self._mm = None
        if self._file is not None:
            self._file.close()
            self._file = None

    def set_car(self, lat: float, lon: float, heading: float, t_ms: int = 0) -> None:
        self._send({"op": "car", "lat": lat, "lon": lon, "heading": heading, "t_ms": t_ms})

    def set_route(self, polyline6: str, congestion: str = "") -> None:
        self._send({"op": "route", "polyline6": polyline6, "congestion": congestion})

    def camera(self, mode: str) -> None:
        self._send({"op": "camera", "mode": mode})

    def pause(self) -> None:
        self._send({"op": "fps", "value": 0})

    def resume(self) -> None:
        self._send({"op": "fps", "value": 30})

    def read(self) -> tuple[int, int, int, int, bytes]:
        if self._mm is None:
            raise RuntimeError("not connected")
        for _ in range(READ_RETRIES):
            seq, width, height, stride, _flags, _pad = HEADER.unpack_from(self._mm, 0)
            start = HEADER_SIZE
            data = bytes(self._mm[start:start + stride * height])
            if HEADER.unpack_from(self._mm, 0)[0] == seq:
                return seq, width, height, stride, data
        raise RuntimeError("map frame kept changing")

    def _send(self, payload: dict) -> None:
        if self._sock is None:
            raise RuntimeError("not connected")
        self._sock.sendall((json.dumps(payload) + "\n").encode())


class MapServiceLink:
    """Pauses the service while the thermal governor asks for it and tells the UI whether a map is live."""

    def __init__(self, engine, client, *, poll_s: float = 1.0):
        self.engine = engine
        self.client = client
        self.poll_s = poll_s
        self.running = False
        self.paused = False
        self.live: bool | None = None
        self.task: asyncio.Task | None = None

    def enabled(self) -> bool:
        cfg = getattr(self.engine, "cfg", None)
        return bool(cfg is not None and cfg.flag("MAP_SERVICE", False))

    def start(self) -> None:
        if not self.enabled():
            self.sync()
            return
        try:
            self.client.connect()
            self.running = True
        except (OSError, RuntimeError) as exc:
            log.warning("map service unavailable: %s", type(exc).__name__)
            self.running = False
        self.sync()
        if self.running:
            self.task = asyncio.get_running_loop().create_task(self._watch())

    def sync(self) -> bool:
        """Applies the thermal flag to the service and publishes ``map_live``. Returns the live state."""
        hot = bool(getattr(self.engine, "thermal_flags", {}).get("map_service_off"))
        if self.enabled() and self.running:
            try:
                if hot and not self.paused:
                    self.client.pause()
                    self.paused = True
                elif not hot and self.paused:
                    self.client.resume()
                    self.paused = False
            except (OSError, RuntimeError) as exc:
                log.warning("map service control failed: %s", type(exc).__name__)
                self.running = False
        live = self.enabled() and self.running and not self.paused
        if live != self.live:
            self.live = live
            self.engine.emit("map_live", live)
        return live

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(self.poll_s)
            self.sync()

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.running = False
        self.sync()
        self.client.close()


def serve_fake(shm_path: Path, sock_path: Path, *, width: int = 64, height: int = 48) -> None:
    """Test double: a solid frame whose sequence climbs until fps is 0."""
    stride = width * 4
    size = HEADER_SIZE + stride * height
    shm_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(shm_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.ftruncate(fd, size)
    mm = mmap.mmap(fd, size)
    pixels = bytes([40, 80, 160, 255]) * (width * height)
    mm[HEADER_SIZE:HEADER_SIZE + len(pixels)] = pixels
    HEADER.pack_into(mm, 0, 1, width, height, stride, 0, 0)
    if sock_path.exists():
        sock_path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)
    server.settimeout(5)
    conn, _addr = server.accept()
    conn.settimeout(0.05)
    seq = 1
    paused = False
    buf = b""
    try:
        while True:
            if not paused:
                seq += 1
                HEADER.pack_into(mm, 0, seq, width, height, stride, 0, 0)
            try:
                chunk = conn.recv(4096)
            except TimeoutError:
                chunk = b""
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line:
                        continue
                    msg = json.loads(line)
                    if msg.get("op") == "fps":
                        paused = int(msg.get("value") or 0) <= 0
            time.sleep(0.05)
    finally:
        conn.close()
        server.close()
        mm.close()
        os.close(fd)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--shm", required=True)
    parser.add_argument("--sock", required=True)
    parser.add_argument("--fake", action="store_true")
    args = parser.parse_args()
    if args.fake:
        serve_fake(Path(args.shm), Path(args.sock))


if __name__ == "__main__":
    main()
