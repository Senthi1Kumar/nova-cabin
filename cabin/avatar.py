"""Avatar asset loading: portrait + rig landmarks → QML-ready values."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QVector2D, QVector4D


def load_rig(folder: Path) -> dict:
    r = json.loads((folder / "rig.json").read_text())
    m, (el, er), b, h = r["mouth"], r["eyes"], r["brows"], r["head"]

    def eye(e):
        return (QVector4D(e["cx"], e["cy"], e["half_w"], e["half_h"]),
                QVector4D(e["lid_px"], e.get("lash_px", 5), *e["gaze_px"]))

    eL, eLx = eye(el)
    eR, eRx = eye(er)
    return {
        "image": QUrl.fromLocalFile(str(folder / r["image"])),
        "imgSize": QVector2D(*r["size"]),
        "mouthA": QVector4D(m["cx"], m["seam_center"], m["seam_corner"], m["half_width"]),
        "mouthB": QVector4D(m["jaw_px"], m["chin"], 0, 0),
        "eyeL": eL, "eyeLx": eLx, "eyeR": eR, "eyeRx": eRx,
        "browR": QVector4D(b["cx"], b["cy"], b["sx"], b["sy"]),
        "browPx": float(b["raise_px"]),
        "headR": QVector4D(h["pivot"][0], h["pivot"][1], h["neck_top"], h["neck_bottom"]),
    }
