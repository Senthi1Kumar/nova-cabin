import QtQuick

// Rounded-corner image that works on every scenegraph backend (GL, Vulkan,
// software). Canvas clips once per source change, so cost is negligible.
Canvas {
    id: root
    property url source
    property real radius: 16
    property real overlayTop: 0.0     // subtle top sheen 0..1
    property real overlayBottom: 0.0  // bottom vignette 0..1
    renderStrategy: Canvas.Cooperative
    onSourceChanged: { if (source != "") loadImage(source) ; requestPaint() }
    onImageLoaded: requestPaint()
    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()
    Component.onCompleted: if (source != "") loadImage(source)
    Image { id: probe; source: root.source; visible: false; onStatusChanged: root.requestPaint() }

    onPaint: {
        const ctx = getContext("2d")
        ctx.reset()
        const r = Math.min(radius, width / 2, height / 2)
        ctx.beginPath()
        ctx.moveTo(r, 0); ctx.lineTo(width - r, 0)
        ctx.arcTo(width, 0, width, r, r); ctx.lineTo(width, height - r)
        ctx.arcTo(width, height, width - r, height, r); ctx.lineTo(r, height)
        ctx.arcTo(0, height, 0, height - r, r); ctx.lineTo(0, r)
        ctx.arcTo(0, 0, r, 0, r); ctx.closePath()
        ctx.save(); ctx.clip()
        if (isImageLoaded(source)) {
            // cover-fit
            const iw = probe.implicitWidth || width, ih = probe.implicitHeight || height
            const s = Math.max(width / iw, height / ih)
            const dw = iw * s, dh = ih * s
            ctx.drawImage(source, (width - dw) / 2, (height - dh) / 2, dw, dh)
        } else {
            ctx.fillStyle = "#1a2448"; ctx.fill()
        }
        if (overlayBottom > 0) {
            const g = ctx.createLinearGradient(0, height * 0.55, 0, height)
            g.addColorStop(0, "rgba(6,10,24,0)"); g.addColorStop(1, "rgba(6,10,24," + overlayBottom + ")")
            ctx.fillStyle = g; ctx.fillRect(0, 0, width, height)
        }
        if (overlayTop > 0) {
            const g2 = ctx.createLinearGradient(0, 0, 0, height * 0.4)
            g2.addColorStop(0, "rgba(255,255,255," + overlayTop + ")"); g2.addColorStop(1, "rgba(255,255,255,0)")
            ctx.fillStyle = g2; ctx.fillRect(0, 0, width, height)
        }
        ctx.restore()
    }
}
