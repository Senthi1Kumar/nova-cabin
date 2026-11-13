"""Vector icon set + Qt image provider.

All glyphs are drawn on a 24x24 grid with a 1.6 px round stroke so they share
one optical weight across the status bar, cards and dock. Colour is injected
at request time:  image://icons/<name>?c=<hex>&w=<stroke>
"""
from __future__ import annotations

from urllib.parse import parse_qs

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtQuick import QQuickImageProvider
from PySide6.QtSvg import QSvgRenderer

_S = 'fill="none" stroke="{c}" stroke-width="{w}" stroke-linecap="round" stroke-linejoin="round"'
_F = 'fill="{c}" stroke="none"'

ICONS: dict[str, str] = {
    "home": f'<path {_S} d="M3.5 10.6 12 3.5l8.5 7.1V19.5a1.5 1.5 0 0 1-1.5 1.5h-4.2v-5.6H9.2V21H5a1.5 1.5 0 0 1-1.5-1.5z"/>',
    "grid": f'<g {_S}><rect x="3.5" y="3.5" width="7" height="7" rx="2"/><rect x="13.5" y="3.5" width="7" height="7" rx="2"/>'
            f'<rect x="3.5" y="13.5" width="7" height="7" rx="2"/><rect x="13.5" y="13.5" width="7" height="7" rx="2"/></g>',
    "music": f'<g {_S}><path d="M9 18V5.2l11-2.2v12.6"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="15.6" r="3"/></g>',
    "nav": f'<path {_S} d="M12 2.8 20.2 21 12 16.8 3.8 21z"/>',
    "car": f'<g {_S}><path d="M5 11 6.7 6.6A2 2 0 0 1 8.6 5.3h6.8a2 2 0 0 1 1.9 1.3L19 11"/>'
           f'<path d="M3.8 11h16.4a1.3 1.3 0 0 1 1.3 1.3v4.2H2.5v-4.2A1.3 1.3 0 0 1 3.8 11z"/>'
           f'<path d="M5.5 16.5v2.2M18.5 16.5v2.2M6.8 13.7h1.4M15.8 13.7h1.4"/></g>',
    "phone": f'<path {_S} d="M21 16.4v2.9a1.9 1.9 0 0 1-2.1 1.9 18.8 18.8 0 0 1-8.2-2.9 18.5 18.5 0 0 1-5.7-5.7A18.8 18.8 0 0 1 2.1 4.4 1.9 1.9 0 0 1 4 2.3h2.9a1.9 1.9 0 0 1 1.9 1.6c.1.9.3 1.8.7 2.7a1.9 1.9 0 0 1-.4 2L7.9 9.8a15.2 15.2 0 0 0 5.7 5.7l1.2-1.2a1.9 1.9 0 0 1 2-.4c.9.3 1.8.6 2.7.7a1.9 1.9 0 0 1 1.5 1.8z"/>',
    "video": f'<g {_S}><rect x="2.5" y="6" width="13.5" height="12" rx="2.5"/><path d="m16 10.5 5.5-3.3v9.6L16 13.5"/></g>',
    "seat": f'<g {_S}><path d="M7.2 3.2c1.4 0 2.2 1 2.4 2.2l1.2 8.1h6.3a2 2 0 0 1 2 2v2.2H9.8a2 2 0 0 1-2-1.7L5.8 6.3c-.2-1.6.3-3.1 1.4-3.1z"/><path d="M10.5 21h8.5"/></g>',
    "seat_heat": f'<g {_S}><path d="M5.2 6.2c1.3 0 2 .9 2.2 2l1 6.6h5.4a1.8 1.8 0 0 1 1.8 1.8V18.5H8.1a1.8 1.8 0 0 1-1.8-1.5L4 8.8c-.2-1.4.2-2.6 1.2-2.6z"/>'
                 f'<path d="M8.5 21.2h8M13.5 2.5c-.9.9-.9 1.8 0 2.7s.9 1.8 0 2.7M17 2.5c-.9.9-.9 1.8 0 2.7s.9 1.8 0 2.7M20.5 2.5c-.9.9-.9 1.8 0 2.7s.9 1.8 0 2.7"/></g>',
    "fan": f'<g {_S}><path d="M10.8 16.4a6.1 6.1 0 0 1-8.6-7l5.4 1.4a6.1 6.1 0 0 1 7-8.6l-1.4 5.4a6.1 6.1 0 0 1 8.6 7l-5.4-1.4a6.1 6.1 0 0 1-7 8.6z"/><circle cx="12" cy="12" r=".6"/></g>',
    "mic": f'<g {_S}><rect x="9" y="2.5" width="6" height="11.5" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5v3.5M9 21h6"/></g>',
    "keyboard": f'<g {_S}><rect x="2.5" y="5.5" width="19" height="13" rx="2.5"/>'
                f'<path d="M6.5 9.3h.01M10 9.3h.01M14 9.3h.01M17.5 9.3h.01M6.5 12.3h.01M10 12.3h.01M14 12.3h.01M17.5 12.3h.01M8 15.4h8"/></g>',
    "send": f'<g {_S}><path d="M21.2 2.8 2.8 10.4l7.7 3.1 3.1 7.7z"/><path d="m10.5 13.5 4.6-4.6"/></g>',
    "calendar": f'<g {_S}><rect x="3.5" y="4.8" width="17" height="16" rx="2.5"/><path d="M8 2.8v4M16 2.8v4M3.5 9.8h17M8 13.8h.01M12 13.8h.01M16 13.8h.01M8 17.2h.01M12 17.2h.01"/></g>',
    "share": f'<path {_S} d="M13.8 4.6 20.8 11l-7 6.4v-3.7c-4.8 0-8.2 1.4-10.6 4.7.9-5.3 3.9-9.2 10.6-9.9z"/>',
    "route": f'<g {_S}><circle cx="6.5" cy="17.5" r="2.6"/><path d="M15.5 9.8c-1.6-1.8-2.7-3.2-2.7-4.6a2.7 2.7 0 0 1 5.4 0c0 1.4-1.1 2.8-2.7 4.6z"/>'
             f'<path d="M15.5 12.5v.5a3 3 0 0 1-3 3h-2.4"/></g>',
    "bolt": f'<path {_S} d="M13.2 2.5 4.8 13.4h6.8l-1 8.1 8.6-11.1h-6.9z"/>',
    "battery": f'<g {_S}><rect x="2.5" y="7.2" width="16.5" height="9.6" rx="2.2"/><path d="M21.5 10.6v2.8"/></g>'
               f'<rect {_F} x="4.6" y="9.3" width="8.2" height="5.4" rx="1"/>',
    "cloud": f'<path {_S} d="M7.2 18.8h10.3a4.1 4.1 0 0 0 .6-8.1 6 6 0 0 0-11.5-1.5 4.8 4.8 0 0 0 .6 9.6z"/>',
    "bell": f'<g {_S}><path d="M6 9.2a6 6 0 0 1 12 0c0 5.9 2.5 7.6 2.5 7.6h-17S6 15.1 6 9.2z"/><path d="M10.2 20.2a2 2 0 0 0 3.6 0"/></g>',
    "bluetooth": f'<path {_S} d="m6.8 7.2 10.4 9.6-5.2 4.7V2.5l5.2 4.7L6.8 16.8"/>',
    "wifi": f'<g {_S}><path d="M2.5 9a14.5 14.5 0 0 1 19 0M5.6 12.6a9.8 9.8 0 0 1 12.8 0M8.8 16.2a5 5 0 0 1 6.4 0"/><path d="M12 19.8h.01" stroke-width="2.6"/></g>',
    "signal": f'<g {_F}><rect x="3" y="15" width="3" height="5" rx=".8"/><rect x="8" y="11.5" width="3" height="8.5" rx=".8"/>'
              f'<rect x="13" y="8" width="3" height="12" rx=".8"/><rect x="18" y="4" width="3" height="16" rx=".8"/></g>',
    "user": f'<g {_F}><circle cx="12" cy="8" r="4.2"/><path d="M3.8 20.6a8.2 8.2 0 0 1 16.4 0z"/></g>',
    "chev_l": f'<path {_S} d="m14.5 18-6-6 6-6"/>',
    "chev_r": f'<path {_S} d="m9.5 18 6-6-6-6"/>',
    "pulse": f'<path {_S} d="M1.5 12h3.2l1.6-3.4 2 7.4 2.1-12.5 2.2 17 2-11.6 1.6 6.4 1.4-3.3h4.9"/>',
    "close": f'<path {_S} d="M6 6l12 12M18 6 6 18"/>',
    "globe": f'<g {_S}><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.6 2.5 3.9 5.5 3.9 9s-1.3 6.5-3.9 9c-2.6-2.5-3.9-5.5-3.9-9S9.4 5.5 12 3z"/></g>',
    "pin": f'<g {_S}><path d="M12 21.2s-6.8-6-6.8-11.2a6.8 6.8 0 0 1 13.6 0c0 5.2-6.8 11.2-6.8 11.2z"/><circle cx="12" cy="10" r="2.5"/></g>',
    "clock": f'<g {_S}><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.2 2"/></g>',
    "flag": f'<g {_S}><path d="M5 21V4M5 4.5c4.5-2.5 8.5 2.5 14 0v9c-5.5 2.5-9.5-2.5-14 0"/></g>',
    "arrow_up": f'<path {_S} d="M12 20V5M5.5 11.5 12 5l6.5 6.5"/>',
    "sparkle": f'<path {_S} d="M12 3.5c.6 4.4 3.1 6.9 7.5 7.5-4.4.6-6.9 3.1-7.5 7.5-.6-4.4-3.1-6.9-7.5-7.5 4.4-.6 6.9-3.1 7.5-7.5z"/>',
    "stop": f'<rect {_F} x="6.5" y="6.5" width="11" height="11" rx="2.5"/>',
}


def svg(name: str, color: str = "#E8EEFF", width: float = 1.6) -> bytes:
    body = ICONS[name].format(c=color, w=width)
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">{body}</svg>'.encode()


class IconProvider(QQuickImageProvider):
    def __init__(self) -> None:
        super().__init__(QQuickImageProvider.ImageType.Image)
        self._cache: dict[tuple, QImage] = {}

    def requestImage(self, id_: str, size: QSize, requested: QSize) -> QImage:  # noqa: N802
        name, _, query = id_.partition("?")
        q = parse_qs(query)
        color = q.get("c", ["#E8EEFF"])[0]
        if not color.startswith("#"):
            color = "#" + color
        width = float(q.get("w", ["1.6"])[0])
        w = requested.width() if requested.width() > 0 else 48
        h = requested.height() if requested.height() > 0 else w
        key = (name, color, width, w, h)
        img = self._cache.get(key)
        if img is None:
            img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(Qt.GlobalColor.transparent)
            r = QSvgRenderer(QByteArray(svg(name if name in ICONS else "close", color, width)))
            p = QPainter(img)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            r.render(p, QRectF(0, 0, w, h))
            p.end()
            self._cache[key] = img
        if size is not None:
            size.setWidth(w)
            size.setHeight(h)
        return img
