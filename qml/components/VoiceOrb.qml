import QtQuick
import QtQuick.Shapes
import ".."

// The assistant's "presence": a glass orb with a living core. No glyph —
// state is carried entirely by light and motion.
//   idle       slow violet breathing, orbits drift
//   listening  cool cyan core swells with the driver's voice, ripples out
//   thinking   orbits accelerate and tighten, core dims
//   speaking   warm white core pulses with the assistant's voice
// Tap: talk · end turn · interrupt (handled by the engine).
Item {
    id: root
    property string mode: "idle"
    property real level: 0.0
    signal pressed()
    width: 132; height: 132

    readonly property color coreColor: mode === "listening" ? "#59D2FF"
                                     : mode === "speaking"  ? "#E9DDFF"
                                     : mode === "thinking"  ? "#8E7BFF" : "#9A86FF"
    readonly property real spin: mode === "thinking" ? 3.2 : mode === "idle" ? 0.35 : 1.0
    property real phase: 0
    property real breathe: 0
    FrameAnimation {
        running: root.visible
        onTriggered: {
            root.phase += frameTime * 60 * root.spin
            root.breathe = 0.5 + 0.5 * Math.sin(Date.now() / 1000 * 1.6)
        }
    }

    Image {   // bloom
        anchors.centerIn: parent
        width: 330; height: width
        scale: 0.92 + root.level * 0.22 + (root.mode === "idle" ? root.breathe * 0.05 : 0)
        source: Theme.asset("glow_orb.png")
        opacity: root.mode === "idle" ? 0.5 : 0.95
        Behavior on opacity { NumberAnimation { duration: Theme.durSlow } }
    }
    Image {   // ring halo
        anchors.centerIn: parent
        width: 300; height: 300
        source: Theme.asset("glow_ring.png")
        opacity: 0.55 + root.breathe * 0.3
    }
    Repeater {   // listening ripples
        model: 2
        Rectangle {
            id: rip
            anchors.centerIn: parent
            width: root.width; height: width; radius: width / 2
            color: "transparent"; border.width: 2; border.color: "#7FDBFF"
            opacity: 0; visible: root.mode === "listening"
            SequentialAnimation {
                running: root.mode === "listening"; loops: Animation.Infinite
                PauseAnimation { duration: index * 900 }
                ParallelAnimation {
                    NumberAnimation { target: rip; property: "width"; from: root.width; to: root.width * 1.9; duration: 1800; easing.type: Easing.OutCubic }
                    NumberAnimation { target: rip; property: "opacity"; from: 0.55; to: 0; duration: 1800 }
                }
            }
        }
    }

    // glass body
    Rectangle {
        id: disc
        anchors.centerIn: parent
        width: root.width; height: width; radius: width / 2
        scale: 1 + root.level * 0.05
        clip: true
        gradient: Gradient {
            GradientStop { position: 0; color: "#24245E" }
            GradientStop { position: 1; color: "#0E1033" }
        }

        // living core — two crossfading light sprites
        Image {
            anchors.centerIn: parent
            width: parent.width * (0.95 + root.level * 0.75 + root.breathe * 0.06); height: width
            source: Theme.asset("amb_violet.png")
            opacity: root.mode === "listening" ? 0.25 : 0.9
            Behavior on opacity { NumberAnimation { duration: Theme.durSlow } }
        }
        Image {
            anchors.centerIn: parent
            width: parent.width * (0.85 + root.level * 0.9); height: width
            source: Theme.asset("amb_cyan.png")
            opacity: root.mode === "listening" ? 0.95 : 0.0
            Behavior on opacity { NumberAnimation { duration: Theme.durSlow } }
        }
        Rectangle {     // hot centre
            anchors.centerIn: parent
            width: 10 + root.level * 26 + (root.mode === "thinking" ? 0 : root.breathe * 3); height: width; radius: width / 2
            color: root.coreColor
            opacity: root.mode === "thinking" ? 0.35 : 0.75
            Behavior on color { ColorAnimation { duration: Theme.durSlow } }
        }

        // orbits
        Repeater {
            model: [ { r: 0.40, sweep: 130, w: 2.2, dir:  1.0, a: 0.85 },
                     { r: 0.31, sweep: 95,  w: 1.6, dir: -1.6, a: 0.65 },
                     { r: 0.22, sweep: 70,  w: 1.2, dir:  2.4, a: 0.50 } ]
            Shape {
                anchors.fill: parent
                preferredRendererType: Shape.CurveRenderer
                rotation: root.phase * modelData.dir + index * 120
                scale: root.mode === "thinking" ? 0.88 : 1.0 + (root.mode === "listening" ? root.level * 0.12 : 0)
                Behavior on scale { NumberAnimation { duration: Theme.durMed; easing.type: Easing.OutCubic } }
                opacity: modelData.a
                ShapePath {
                    strokeWidth: modelData.w
                    strokeColor: Qt.lighter(root.coreColor, 1.15)
                    fillColor: "transparent"
                    capStyle: ShapePath.RoundCap
                    PathAngleArc { centerX: disc.width / 2; centerY: disc.height / 2
                                   radiusX: disc.width * modelData.r; radiusY: disc.height * modelData.r
                                   startAngle: 0; sweepAngle: modelData.sweep }
                }
            }
        }
    }

    // conical rim
    Shape {
        anchors.fill: disc
        scale: disc.scale
        preferredRendererType: Shape.CurveRenderer
        rotation: root.phase * 0.5
        ShapePath {
            strokeWidth: 0; strokeColor: "transparent"
            fillGradient: ConicalGradient {
                centerX: disc.width / 2; centerY: disc.height / 2; angle: 90
                GradientStop { position: 0.00; color: "#C08BFF" }
                GradientStop { position: 0.35; color: "#6E7CFF" }
                GradientStop { position: 0.60; color: "#4CB8FF" }
                GradientStop { position: 0.85; color: "#8E7BFF" }
                GradientStop { position: 1.00; color: "#C08BFF" }
            }
            PathAngleArc { centerX: disc.width / 2; centerY: disc.height / 2
                           radiusX: disc.width / 2; radiusY: disc.height / 2; startAngle: 0; sweepAngle: 360 }
            PathAngleArc { centerX: disc.width / 2; centerY: disc.height / 2
                           radiusX: disc.width / 2 - 4; radiusY: disc.height / 2 - 4; startAngle: 0; sweepAngle: -360 }
        }
    }
    Rectangle {   // glass sheen
        anchors.horizontalCenter: disc.horizontalCenter; y: disc.y + 8
        width: disc.width * 0.62; height: disc.height * 0.30; radius: height / 2
        gradient: Gradient {
            GradientStop { position: 0; color: Qt.rgba(1, 1, 1, 0.10) }
            GradientStop { position: 1; color: "transparent" }
        }
    }
    MouseArea { anchors.fill: disc; anchors.margins: -20; onClicked: root.pressed() }
}
