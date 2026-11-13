import QtQuick
import ".."

Item {
    id: root
    property string icon
    property string label
    property string value
    property bool divider: true
    property color iconColor: Theme.iconInk
    implicitHeight: 56

    Icon { id: ic; name: root.icon; size: 30; color: root.iconColor
           x: 22; anchors.verticalCenter: parent.verticalCenter }
    Text {
        x: 80; anchors.verticalCenter: parent.verticalCenter
        width: parent.width - x - 20; elide: Text.ElideRight
        textFormat: Text.StyledText
        text: "<font color='" + Theme.textDim + "'>" + root.label + "</font>&nbsp;&nbsp;" + root.value
        color: Theme.text
        font.family: Theme.family; font.pixelSize: 23; font.features: { "tnum": 1 }
    }
    Rectangle {
        visible: root.divider
        anchors.bottom: parent.bottom; x: 20; width: parent.width - 40; height: 1
        color: Theme.hairline
    }
}
