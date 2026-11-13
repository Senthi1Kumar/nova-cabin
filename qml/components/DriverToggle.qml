import QtQuick
import ".."

// A labelled switch the driver has to flip by hand (consent).
Item {
    id: root
    property bool checked: false
    property string text: ""
    signal toggled(bool value)

    implicitHeight: 64
    implicitWidth: row.implicitWidth

    Row {
        id: row
        spacing: 18
        anchors.verticalCenter: parent.verticalCenter
        Rectangle {
            width: 84; height: 48; radius: 24
            anchors.verticalCenter: parent.verticalCenter
            color: root.checked ? Theme.action : Qt.rgba(1, 1, 1, 0.10)
            border.width: 1
            border.color: root.checked ? Theme.action : Theme.hairlineHi
            Behavior on color { ColorAnimation { duration: Theme.durFast } }
            Rectangle {
                width: 38; height: 38; radius: 19
                y: 5
                x: root.checked ? parent.width - width - 5 : 5
                color: root.checked ? "#1A1206" : Theme.textDim
                Behavior on x { NumberAnimation { duration: Theme.durFast } }
            }
        }
        Text {
            width: Math.min(implicitWidth, root.width - 120)
            anchors.verticalCenter: parent.verticalCenter
            text: root.text
            wrapMode: Text.WordWrap
            color: Theme.text
            font.family: Theme.family
            font.pixelSize: Theme.fsCaption + 2
        }
    }
    MouseArea { anchors.fill: parent; onClicked: root.toggled(!root.checked) }
}
