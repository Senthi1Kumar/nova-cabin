import QtQuick
import ".."

// The luminous horizon that grounds the avatar. Canvas gives us gradient
// strokes + true blur glow on every backend; repaint is throttled to 30 fps
// and only while the assistant is active.
Canvas {
    id: root
    property real energy: 0.3       // 0 idle … 1 speaking
    property real phase: 0
    property bool animate: true
    property bool cheap: false        // no shadow blur (software rasteriser)
    renderStrategy: Canvas.Cooperative
    renderTarget: Canvas.FramebufferObject

    Timer { interval: 33; repeat: true; running: root.animate && root.visible
            onTriggered: { root.phase += 0.012 + root.energy * 0.03; root.requestPaint() } }
    onEnergyChanged: requestPaint()
    onWidthChanged: requestPaint()

    function crest(t) {   // y offset along the curve, small breathing motion
        return Math.sin(root.phase * 2.0 + t * 6.0) * (3 + root.energy * 9)
    }
    function wavePath(ctx, w, h, k, off) {
        // rises from the left edge, crests under the shoulder line, then
        // settles and dives toward the voice controls
        ctx.moveTo(-20, h * 0.42 + off)
        ctx.bezierCurveTo(w * 0.12, h * (0.23 - k) + crest(0.1) + off,
                          w * 0.20, h * 0.15 + crest(0.25) + off,
                          w * 0.38, h * 0.28 + crest(0.4) + off)
        ctx.bezierCurveTo(w * 0.56, h * 0.41 + crest(0.6) + off,
                          w * 0.76, h * 0.45 + crest(0.8) + off,
                          w + 20, h * 0.70 + off)
    }
    onPaint: {
        const ctx = getContext("2d")
        const w = width, h = height
        ctx.reset()

        // 1. ground mass (occludes the avatar's lower edge)
        ctx.beginPath()
        wavePath(ctx, w, h, 0.0, 2)
        ctx.lineTo(w + 20, h + 10); ctx.lineTo(-20, h + 10); ctx.closePath()
        let g = ctx.createLinearGradient(0, h * 0.1, 0, h)
        g.addColorStop(0, "rgba(13,20,52,0.96)")
        g.addColorStop(0.5, "rgba(9,14,36,0.98)")
        g.addColorStop(1, "rgba(6,9,22,1)")
        ctx.fillStyle = g; ctx.fill()

        // 2. glow passes, wide → tight
        const passes = [
            { lw: 34, a: 0.14, blur: 44 },
            { lw: 14, a: 0.30, blur: 24 },
            { lw: 5, a: 0.80, blur: 12 },
            { lw: 2.2, a: 1.00, blur: 4 }
        ]
        for (let p of passes) {
            ctx.beginPath(); wavePath(ctx, w, h, 0.0, 0)
            const s = ctx.createLinearGradient(0, 0, w, 0)
            s.addColorStop(0.00, "rgba(70,120,255,0)")
            s.addColorStop(0.08, "rgba(80,130,255," + p.a + ")")
            s.addColorStop(0.24, "rgba(150,195,255," + p.a + ")")
            s.addColorStop(0.55, "rgba(150,110,255," + (p.a * (0.7 + root.energy * 0.3)) + ")")
            s.addColorStop(0.85, "rgba(120,90,255," + (p.a * 0.35) + ")")
            s.addColorStop(1.00, "rgba(120,90,255,0)")
            ctx.strokeStyle = s; ctx.lineWidth = p.lw; ctx.lineCap = "round"
            ctx.shadowBlur = root.cheap ? 0 : p.blur; ctx.shadowColor = "rgba(110,140,255,0.8)"
            ctx.stroke()
        }
        // 3. secondary violet filament, slightly offset and phase-shifted
        ctx.shadowBlur = root.cheap ? 0 : 8; ctx.shadowColor = "rgba(170,110,255,0.7)"
        ctx.beginPath(); wavePath(ctx, w, h, 0.05, 14 + Math.sin(root.phase * 1.3) * 4)
        const s2 = ctx.createLinearGradient(0, 0, w, 0)
        s2.addColorStop(0.05, "rgba(170,110,255,0)"); s2.addColorStop(0.35, "rgba(170,120,255,0.45)")
        s2.addColorStop(0.75, "rgba(170,120,255,0.15)"); s2.addColorStop(1, "rgba(170,120,255,0)")
        ctx.strokeStyle = s2; ctx.lineWidth = 1.4; ctx.stroke()

        // 4. dissolve the right end into the scene (no hard canvas edge)
        ctx.shadowBlur = 0
        ctx.globalCompositeOperation = "destination-out"
        const f = ctx.createLinearGradient(w * 0.70, 0, w, 0)
        f.addColorStop(0, "rgba(0,0,0,0)"); f.addColorStop(1, "rgba(0,0,0,1)")
        ctx.fillStyle = f; ctx.fillRect(0, 0, w, h)
        ctx.globalCompositeOperation = "source-over"
    }
}
