"""The DriveOS Face ID preview, drawn into the checkout card.

Frames stay in the DriveOS preview hub (RAM only). This module copies one into a Qt image.
"""
from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickImageProvider


def face_caption() -> dict[str, str]:
    """The line under the camera: scanning, verified with the driver's name, or a miss."""
    state, name = "scanning", ""
    try:
        from driveos.biometrics.preview import HUB
        snap = HUB.snapshot()
        state = str(snap.get("face_state") or "scanning")
        name = str(snap.get("face_name") or "")
    except Exception:
        state = "scanning"
    if state == "verified":
        line = f"Face verified · {name}" if name else "Face verified"
    elif state == "failed":
        line = "Face didn’t match"
    else:
        line = "Look at the camera"
    return {"state": state, "line": line}


def _decode(frame: bytes) -> QImage | None:
    from driveos.biometrics.preview import HEADER_SIZE, parse_header
    header = parse_header(frame)
    if header is None or header.width <= 0 or header.height <= 0:
        return None
    need = header.width * header.height * 2
    payload = frame[HEADER_SIZE:HEADER_SIZE + need]
    if len(payload) < need:
        return None
    img = QImage(payload, header.width, header.height, header.width * 2, QImage.Format.Format_RGB16)
    return img.copy()


class FacePreviewProvider(QQuickImageProvider):
    """`image://facepreview/frame`. The newest camera square, or a blank tile."""

    def __init__(self) -> None:
        super().__init__(QQuickImageProvider.ImageType.Image)
        self._blank = QImage(2, 2, QImage.Format.Format_RGB32)
        self._blank.fill(0xFF14182A)

    def requestImage(self, _id: str, size: QSize, requested: QSize) -> QImage:  # noqa: N802
        frame = None
        try:
            from driveos.biometrics.preview import HUB
            frame = HUB.last_frame
        except Exception:
            frame = None
        img = _decode(frame) if frame else None
        if img is None:
            img = self._blank
        if size is not None:
            size.setWidth(img.width())
            size.setHeight(img.height())
        return img
