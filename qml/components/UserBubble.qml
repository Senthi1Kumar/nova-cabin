import QtQuick
import QtQuick.Shapes
import ".."

// Driver utterance. Tail sits bottom-right, pointing at the profile chip.
Item {
    id: root
    property string text
    property bool partial: false     // live STT hypothesis
    property real maxTextWidth: 640
    readonly property real padH: 28
    readonly property real padV: 18
    readonly property real tail: 18
    readonly property real r: 24
    readonly property real bw: width - tail

    implicitWidth: label.width + padH * 2 + tail
    implicitHeight: label.height + padV * 2

    Shape {
        anchors.fill: parent
        preferredRendererType: Shape.CurveRenderer
        ShapePath {
            strokeWidth: 1
            strokeColor: Qt.rgba(0.72, 0.80, 1.0, 0.38)
            fillGradient: LinearGradient {
                x1: 0; y1: 0; x2: 0; y2: root.height
                GradientStop { position: 0; color: Qt.rgba(0.30, 0.38, 0.70, 0.72) }
                GradientStop { position: 1; color: Qt.rgba(0.20, 0.26, 0.54, 0.72) }
            }
            startX: root.r; startY: 0
            PathLine { x: root.bw - root.r; y: 0 }
            PathArc  { x: root.bw; y: root.r; radiusX: root.r; radiusY: root.r }
            PathLine { x: root.bw; y: root.height - 16 }
            PathQuad { x: root.width; y: root.height; controlX: root.bw + 1; controlY: root.height - 1 }   // tail
            PathLine { x: root.r; y: root.height }
            PathArc  { x: 0; y: root.height - root.r; radiusX: root.r; radiusY: root.r }
            PathLine { x: 0; y: root.r }
            PathArc  { x: root.r; y: 0; radiusX: root.r; radiusY: root.r }
        }
    }
    Text {
        id: label
        x: root.padH; y: root.padV
        width: Math.min(implicitWidth, root.maxTextWidth)
        text: root.text + (root.partial ? "<font color='" + Theme.accentHi + "'>&nbsp;▍</font>" : "")
        textFormat: Text.StyledText
        wrapMode: Text.Wrap
        color: root.partial ? "#C9D2F2" : Theme.text; lineHeight: 1.12
        Behavior on color { ColorAnimation { duration: Theme.durMed } }
        font.family: Theme.family; font.pixelSize: Theme.fsBody + 1
    }
}
