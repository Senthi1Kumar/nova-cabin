import QtQuick
import ".."

// Symmetric spectrum bars. Feed `bands` (0..1 array) from the audio engine;
// when empty, a procedural envelope driven by `level` is used.
Item {
    id: root
    property real level: 0.4
    property var bands: []
    property int count: 13
    property bool mirrored: false     // peak on the outer side instead of inner
    property real barWidth: 4
    property real gap: 9
    property real maxH: 52
    property real minH: 6
    property real t: 0
    width: count * barWidth + (count - 1) * gap
    height: maxH

    FrameAnimation { running: root.visible; onTriggered: root.t += frameTime }

    function env(i) {
        const n = root.count - 1
        const x = root.mirrored ? (n - i) / n : i / n          // 0 = outer edge
        const bell = Math.pow(Math.sin(Math.PI * Math.min(1, x * 1.05)), 1.6)
        const tail = 0.25 + 0.75 * x
        return bell * tail
    }
    Repeater {
        model: root.count
        Rectangle {
            readonly property real b: root.bands.length === root.count ? root.bands[index]
                : root.env(index) * (0.35 + 0.65 * root.level)
                  * (0.70 + 0.30 * Math.sin(root.t * (5.0 + index * 0.9) + index * 1.7))
            width: root.barWidth; radius: width / 2
            height: root.minH + (root.maxH - root.minH) * Math.max(0, Math.min(1, b))
            x: index * (root.barWidth + root.gap)
            anchors.verticalCenter: parent.verticalCenter
            readonly property real edge: Math.min(index, root.count - 1 - index) / (root.count / 2)
            opacity: 0.35 + 0.65 * Math.min(1, edge * 1.6)
            gradient: Gradient {
                GradientStop { position: 0; color: "#9C8CFF" }
                GradientStop { position: 1; color: "#4C9BFF" }
            }
        }
    }
}
