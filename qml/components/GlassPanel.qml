import QtQuick
import ".."

// Frosted panel: vertical tint, hairline border and a specular top edge.
Item {
    id: root
    property real radius: Theme.radiusCard
    property color topTint: Theme.glassTop
    property color bottomTint: Theme.glassBottom
    property color borderColor: Theme.hairlineHi
    property real sheen: 0.55            // strength of the lit top edge
    default property alias content: holder.data

    Rectangle {
        anchors.fill: parent
        radius: root.radius
        gradient: Gradient {
            GradientStop { position: 0.0; color: root.topTint }
            GradientStop { position: 1.0; color: root.bottomTint }
        }
        border.width: 1
        border.color: root.borderColor
    }
    // specular edge — fades out toward both ends like light catching a bevel
    Rectangle {
        x: root.radius; y: 0
        width: parent.width - root.radius * 2; height: 1
        opacity: root.sheen
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.0; color: "transparent" }
            GradientStop { position: 0.35; color: "#C9D4FF" }
            GradientStop { position: 0.7; color: "#9FAEFF" }
            GradientStop { position: 1.0; color: "transparent" }
        }
    }
    Item { id: holder; anchors.fill: parent }
}
