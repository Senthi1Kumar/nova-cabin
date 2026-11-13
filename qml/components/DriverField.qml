import QtQuick
import ".."

// Single-line text field in the cabin's pill style.
Rectangle {
    id: root
    property alias text: input.text
    property string placeholder: ""
    property alias maximumLength: input.maximumLength
    property alias validator: input.validator
    property alias inputMethodHints: input.inputMethodHints
    property alias activeFocusInput: input.activeFocus
    signal accepted()
    function focusInput() { input.forceActiveFocus() }

    implicitWidth: 360
    implicitHeight: 64
    radius: height / 2
    color: Qt.rgba(0.07, 0.09, 0.22, 0.95)
    border.width: 1.5
    border.color: input.activeFocus ? Qt.rgba(0.6, 0.66, 1, 0.7) : Theme.hairlineHi

    TextInput {
        id: input
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.leftMargin: 26
        anchors.rightMargin: 26
        anchors.verticalCenter: parent.verticalCenter
        color: Theme.text
        font.family: Theme.family
        font.pixelSize: Theme.fsBody
        selectionColor: Theme.accent
        clip: true
        Keys.onReturnPressed: root.accepted()
        Keys.onEnterPressed: root.accepted()
        Text {
            anchors.fill: parent
            verticalAlignment: Text.AlignVCenter
            visible: input.text === ""
            text: root.placeholder
            color: Theme.textFaint
            font: input.font
        }
    }
    MouseArea { anchors.fill: parent; z: -1; onClicked: input.forceActiveFocus() }
}
