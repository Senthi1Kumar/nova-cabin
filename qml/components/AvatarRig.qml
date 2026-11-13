import QtQuick
import ".."

// Digital human: GPU face rig (shaders/avatar.frag) + behaviour engine.
//
// Behaviour runs once per display frame (FrameAnimation) and drives every
// facial channel through critically-damped springs, so nothing ever snaps:
//
//   idle       slow head drift, wandering gaze with saccades, relaxed smile, blinks 2.5–5.5 s
//   listening  head tilts toward the driver, brows lift, steady eye contact,
//              micro-nods on the driver's speech peaks, fewer blinks
//   thinking   gaze breaks up-left, head lifts, lips press slightly
//   speaking   lip sync from viseme targets (falls back to audio level),
//              head bob + brow emphasis on stressed syllables, blinks at pauses
Item {
    id: root
    property var rig                      // from Python: cabin.avatar.load_rig()
    property string mode: "idle"
    property real level: 0                // smoothed audio envelope
    property real visemeJaw: 0
    property real visemeRound: 0
    property bool visemesLive: false      // true while the TTS/viseme source is active
    property vector2d blinkVec: Qt.vector2d(0, 0)

    readonly property bool gpu: GraphicsInfo.api !== GraphicsInfo.Software
                                && GraphicsInfo.api !== GraphicsInfo.Unknown

    // ── spring-driven channels ─────────────────────────────────
    QtObject {
        id: ch
        property real jaw: 0;      property real jawV: 0
        property real rnd: 0;      property real rndV: 0
        property real smile: 0.3;  property real smileV: 0
        property real brow: 0;     property real browV: 0
        property real gx: 0;       property real gxV: 0
        property real gy: 0;       property real gyV: 0
        property real rot: 0;      property real rotV: 0
        property real hx: 0;       property real hxV: 0
        property real hy: 0;       property real hyV: 0
        property real blink: 0     // shared, with slight per-eye lag below
        property real blinkR: 0
    }

    // behaviour state
    QtObject {
        id: bh
        property real t: 0
        property real nextBlink: 1.8
        property real blinkStart: -10
        property bool doubleBlink: false
        property real nextSaccade: 0.8
        property real gazeTX: 0.15
        property real gazeTY: 0
        property real prevLevel: 0
        property real emphasis: 0
        property real nod: 0
        property string lastMode: "idle"
        property real modeSince: 0
    }

    function spring(x, v, target, omega, dt) {
        // exact critically-damped spring step: stable for any frame time
        const y = x - target
        const e = Math.exp(-omega * dt)
        const c = v + omega * y
        return [target + (y + c * dt) * e, (v - omega * c * dt) * e]
    }
    function noise(t, seed) {      // cheap smooth 1D noise (sum of incommensurate sines)
        return 0.5 * Math.sin(t * 0.61 + seed) + 0.3 * Math.sin(t * 1.37 + seed * 2.1)
             + 0.2 * Math.sin(t * 2.71 + seed * 3.7)
    }
    function rand(a, b) { return a + Math.random() * (b - a) }

    FrameAnimation {
        running: root.visible
        onTriggered: {
            const dt = Math.min(frameTime, 0.05)
            bh.t += dt
            const t = bh.t
            const m = root.mode
            if (m !== bh.lastMode) { bh.lastMode = m; bh.modeSince = t; bh.nextSaccade = t + 0.15 }
            const since = t - bh.modeSince

            // ── emphasis: rising edges of the speech envelope ──
            const dl = root.level - bh.prevLevel
            bh.prevLevel = root.level
            bh.emphasis = Math.max(bh.emphasis * Math.exp(-dt * 5), dl > 0.012 ? Math.min(1, dl * 18) : 0)

            // ── targets per mode ──
            let jawT = 0, rndT = 0, smileT = 0.3, browT = 0
            let rotT = 0.012 * noise(t * 0.5, 1.3), hxT = 2.0 * noise(t * 0.4, 4.1), hyT = 1.5 * noise(t * 0.45, 7.7)
            let blinkMin = 2.5, blinkMax = 5.5

            if (m === "listening") {
                smileT = 0.18
                browT = 0.45 + 0.15 * noise(t * 0.7, 2.2)
                rotT = -0.035 + 0.006 * noise(t * 0.8, 3.3)            // tilt toward the driver
                // micro-nod on speech peaks of the driver
                if (root.level > 0.45 && bh.nod < 0.2) bh.nod = 1
                hyT = 1.5 * noise(t * 0.5, 1.1) + 2.2 * bh.nod
                blinkMin = 3.5; blinkMax = 7.0
            } else if (m === "thinking") {
                smileT = 0.05
                browT = 0.35
                rndT = 0.25; jawT = -0.12                               // lips pressed "hmm"
                rotT = 0.03 + 0.004 * noise(t, 5.5)
                hyT = -3.5 + 0.8 * noise(t * 0.8, 6.6)
                hxT = -1.5
                blinkMin = 2.0; blinkMax = 4.0
            } else if (m === "speaking") {
                smileT = 0.22 + 0.1 * noise(t * 0.3, 8.8)
                const lv = root.level
                if (root.visemesLive) {
                    jawT = root.visemeJaw * (0.75 + 0.45 * lv)
                    rndT = root.visemeRound
                } else {                                               // audio-only fallback
                    jawT = Math.max(0, (lv - 0.08) * 1.15)
                    rndT = 0.25 * Math.sin(t * 7.3)
                }
                browT = 0.12 + 0.75 * bh.emphasis
                rotT = 0.008 * noise(t * 0.9, 2.9) + 0.012 * bh.emphasis * Math.sin(t * 3.1)
                hyT = 1.2 * noise(t * 0.7, 9.1) - 2.6 * bh.emphasis
                hxT = 1.6 * noise(t * 0.5, 3.9)
                blinkMin = 2.2; blinkMax = 4.5
            } else {   // idle
                smileT = 0.32 + 0.08 * noise(t * 0.2, 1.9)
                browT = 0.05 * (1 + noise(t * 0.3, 4.4))
            }
            bh.nod = Math.max(0, bh.nod - dt * 2.2)

            // ── gaze & saccades ──
            if (t >= bh.nextSaccade) {
                if (m === "thinking") {
                    bh.gazeTX = rand(-1.0, -0.6); bh.gazeTY = rand(-1.0, -0.65)
                    bh.nextSaccade = t + rand(0.9, 1.8)
                } else if (m === "listening") {
                    bh.gazeTX = 0.45 + rand(-0.08, 0.08); bh.gazeTY = rand(-0.08, 0.08)   // eye contact
                    bh.nextSaccade = t + rand(0.6, 1.4)
                } else if (m === "speaking") {
                    const avert = Math.random() < 0.18
                    bh.gazeTX = avert ? rand(-0.5, -0.2) : 0.35 + rand(-0.1, 0.1)
                    bh.gazeTY = avert ? rand(-0.4, 0.0) : rand(-0.1, 0.1)
                    bh.nextSaccade = t + (avert ? rand(0.35, 0.7) : rand(0.8, 2.0))
                } else {
                    bh.gazeTX = rand(-0.45, 0.55); bh.gazeTY = rand(-0.35, 0.3)
                    bh.nextSaccade = t + rand(1.0, 3.2)
                }
            }

            // ── blinks ──
            if (t >= bh.nextBlink) {
                bh.blinkStart = t
                bh.doubleBlink = Math.random() < 0.15
                bh.nextBlink = t + rand(blinkMin, blinkMax) + (bh.doubleBlink ? 0.35 : 0)
            }
            // blink on mode change (natural "reset")
            if (since < dt * 1.5 && t - bh.blinkStart > 0.5) bh.blinkStart = t
            function blinkCurve(u) {             // 70 ms close, 40 ms hold, 130 ms open
                if (u < 0) return 0
                if (u < 0.07) return u / 0.07
                if (u < 0.11) return 1
                if (u < 0.24) { const k = (u - 0.11) / 0.13; return 1 - k * k * (3 - 2 * k) }
                return 0
            }
            const bu = t - bh.blinkStart
            let b = blinkCurve(bu)
            if (bh.doubleBlink) b = Math.max(b, blinkCurve(bu - 0.28))
            ch.blink = b
            ch.blinkR = blinkCurve(bu - 0.012) * 0.97 + (bh.doubleBlink ? 0 : 0)
            if (bh.doubleBlink) ch.blinkR = Math.max(ch.blinkR, blinkCurve(bu - 0.292))
            // looking up lifts the lids a little, looking down lowers them
            const lidBias = Math.max(0, ch.gy) * 0.18

            // ── integrate ──
            let r
            r = spring(ch.jaw, ch.jawV, jawT, m === "speaking" ? 34 : 14, dt); ch.jaw = r[0]; ch.jawV = r[1]
            r = spring(ch.rnd, ch.rndV, rndT, 26, dt); ch.rnd = r[0]; ch.rndV = r[1]
            r = spring(ch.smile, ch.smileV, smileT, 4, dt); ch.smile = r[0]; ch.smileV = r[1]
            r = spring(ch.brow, ch.browV, browT, 9, dt); ch.brow = r[0]; ch.browV = r[1]
            r = spring(ch.gx, ch.gxV, bh.gazeTX, 38, dt); ch.gx = r[0]; ch.gxV = r[1]   // saccade-fast
            r = spring(ch.gy, ch.gyV, bh.gazeTY, 38, dt); ch.gy = r[0]; ch.gyV = r[1]
            r = spring(ch.rot, ch.rotV, rotT, 3.2, dt); ch.rot = r[0]; ch.rotV = r[1]
            r = spring(ch.hx, ch.hxV, hxT, 3.0, dt); ch.hx = r[0]; ch.hxV = r[1]
            r = spring(ch.hy, ch.hyV, hyT, m === "speaking" ? 7 : 3.5, dt); ch.hy = r[0]; ch.hyV = r[1]

            root.blinkVec = Qt.vector2d(Math.min(1, ch.blink + lidBias), Math.min(1, ch.blinkR + lidBias))
        }
    }

    // ── GPU renderer ───────────────────────────────────────────
    Image {
        id: portrait
        source: root.rig ? root.rig.image : ""
        visible: !root.gpu                 // software fallback shows the still
        anchors.fill: parent
        fillMode: Image.PreserveAspectFit
        smooth: true; mipmap: true
    }
    // Only instantiated on a GPU scenegraph: a ShaderEffect in the software
    // renderer suppresses its sibling subtree on some Qt versions.
    Loader {
        anchors.fill: parent
        active: root.gpu && root.rig !== undefined
        sourceComponent: ShaderEffect {
            property variant source: portrait
            property real jaw: ch.jaw
            property real rnd: Math.max(-1, Math.min(1, ch.rnd))
            property real smile: Math.max(0, ch.smile)
            property real brow: Math.max(0, ch.brow)
            property vector2d blink: root.blinkVec
            property vector2d gaze: Qt.vector2d(Math.max(-1, Math.min(1, ch.gx)), Math.max(-1, Math.min(1, ch.gy)))
            property real headRot: ch.rot
            property vector2d headOff: Qt.vector2d(ch.hx, ch.hy)
            property vector2d imgSize: root.rig ? root.rig.imgSize : Qt.vector2d(1, 1)
            property vector4d mouthA: root.rig ? root.rig.mouthA : Qt.vector4d(0, 0, 0, 1)
            property vector4d mouthB: root.rig ? root.rig.mouthB : Qt.vector4d(0, 0, 0, 0)
            property vector4d eyeL:  root.rig ? root.rig.eyeL  : Qt.vector4d(0, 0, 1, 1)
            property vector4d eyeLx: root.rig ? root.rig.eyeLx : Qt.vector4d(1, 1, 0, 0)
            property vector4d eyeR:  root.rig ? root.rig.eyeR  : Qt.vector4d(0, 0, 1, 1)
            property vector4d eyeRx: root.rig ? root.rig.eyeRx : Qt.vector4d(1, 1, 0, 0)
            property vector4d browR: root.rig ? root.rig.browR : Qt.vector4d(0, 0, 1, 1)
            property vector4d headR: root.rig ? root.rig.headR : Qt.vector4d(0, 0, 0, 1)
            property real browPx: root.rig ? root.rig.browPx : 0
            fragmentShader: Qt.resolvedUrl("../shaders/avatar.frag.qsb")
        }
    }

    // expose channels for debugging overlays / tests
    readonly property alias channels: ch
}
