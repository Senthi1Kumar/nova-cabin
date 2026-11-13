import QtQuick
import ".."

// Touch target ≥ 60 px tall (ISO 15005 / NHTSA glance guidance).
Item {
    id: root
    property string text
    property string icon
    property bool primary: false
    property bool flashing: false            // remote "press" (tour autopilot)
    signal clicked()
    function flash() { flashing = true; flashTimer.restart() }
    Timer { id: flashTimer; interval: 260; onTriggered: root.flashing = false }

    implicitHeight: 64
    implicitWidth: row.implicitWidth + 64
    scale: ma.pressed || flashing ? 0.96 : 1.0
    Behavior on scale { NumberAnimation { duration: Theme.durFast; easing.type: Easing.OutQuad } }

    // halo (primary only)
    Image {
        visible: root.primary
        anchors.centerIn: parent
        width: parent.width + 70; height: parent.height + 60
        source: Theme.asset("beam_amber.png")
        opacity: 0.35
    }
    Rectangle {
        id: body
        anchors.fill: parent
        radius: height / 2
        border.width: root.primary ? 1.5 : 1
        border.color: root.primary ? Qt.rgba(1.0, 0.74, 0.45, 0.95) : Qt.rgba(0.66, 0.74, 1.0, 0.22)
        gradient: Gradient {
            GradientStop { position: 0.0; color: root.primary ? Qt.rgba(1.0, 0.70, 0.40, 0.30) : Qt.rgba(0.55, 0.63, 1.0, 0.10) }
            GradientStop { position: 1.0; color: root.primary ? Qt.rgba(0.55, 0.36, 0.30, 0.30) : Qt.rgba(0.30, 0.36, 0.70, 0.06) }
        }
    }
    // inner top highlight
    Rectangle {
        x: body.radius; y: 1; height: 1; width: parent.width - body.radius * 2
        opacity: root.primary ? 0.8 : 0.35
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0; color: "transparent" }
            GradientStop { position: 0.5; color: root.primary ? "#FFE2C2" : "#C9D4FF" }
            GradientStop { position: 1; color: "transparent" }
        }
    }
    Rectangle {   // press feedback
        anchors.fill: parent; radius: body.radius
        color: "white"; opacity: ma.pressed || root.flashing ? 0.12 : 0
        Behavior on opacity { NumberAnimation { duration: Theme.durFast } }
    }
    Row {
        id: row
        anchors.centerIn: parent
        spacing: 18
        Icon { name: root.icon; size: 30; anchors.verticalCenter: parent.verticalCenter
               color: root.primary ? "#FFE7CF" : Theme.iconInk; visible: root.icon !== "" }
        Text {
            text: root.text
            font.family: Theme.family; font.pixelSize: Theme.fsBody; font.weight: Font.Medium
            color: root.primary ? "#FFF3E6" : Theme.text
            anchors.verticalCenter: parent.verticalCenter
        }
    }
    MouseArea { id: ma; anchors.fill: parent; onClicked: root.clicked() }
}
