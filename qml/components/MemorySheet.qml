import QtQuick
import ".."

Item {
    id: root
    property var memory: ({})
    readonly property bool available: !!(root.memory && root.memory.available)
    readonly property var fields: (root.memory && root.memory.fields) || []

    Flickable {
        anchors.fill: parent
        contentHeight: col.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        Column {
            id: col
            width: parent.width
            spacing: 14
            Text { text: "What Nova remembers"; color: Theme.text; font.family: Theme.family; font.pixelSize: Theme.fsLead }
            Text {
                width: parent.width
                wrapMode: Text.WordWrap
                color: Theme.textDim
                font.family: Theme.family
                font.pixelSize: Theme.fsBody
                text: !root.available ? "Memory is not available right now."
                      : (root.fields.length === 0 ? "Nothing yet. Nova learns your places and preferences as you talk."
                                                  : "Tap Forget to remove a single item. Nova only sends a short, coarse summary to the assistant.")
            }
            Repeater {
                model: root.fields
                delegate: Rectangle {
                    required property var modelData
                    width: col.width
                    height: 84
                    radius: Theme.radiusInset
                    color: Qt.rgba(1, 1, 1, 0.05)
                    border.width: 1
                    border.color: Theme.hairline
                    Row {
                        anchors.fill: parent
                        anchors.margins: 14
                        spacing: 16
                        Column {
                            width: parent.width - 200
                            anchors.verticalCenter: parent.verticalCenter
                            Text { text: modelData.label || modelData.field; color: Theme.textFaint; font.family: Theme.family; font.pixelSize: Theme.fsCaption }
                            Text {
                                width: parent.width
                                elide: Text.ElideRight
                                text: modelData.value
                                color: Theme.text
                                font.family: Theme.family
                                font.pixelSize: Theme.fsBody
                            }
                        }
                        PillButton {
                            text: "Forget"
                            anchors.verticalCenter: parent.verticalCenter
                            onClicked: assistant.actionTriggered("driver_memory_forget", ({ "field": modelData.field }))
                        }
                    }
                }
            }
            PillButton {
                visible: root.available && root.fields.length > 0
                text: "Forget everything"
                onClicked: assistant.actionTriggered("driver_memory_forget_all", ({}))
            }
        }
    }
}
