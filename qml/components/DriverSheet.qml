import QtQuick
import ".."

// Driver sheet: voice, face, cards and memory. Opened from the status-bar avatar.
GlassPanel {
    id: root
    property var driver: ({ open: false, tab: "voice", state: ({}) })
    readonly property var sheetState: (root.driver && root.driver.state) || ({})
    readonly property var pending: root.sheetState.pending || null
    readonly property string notice: root.sheetState.notice || ""
    signal closed()
    radius: Theme.radiusCard
    topTint: "#111b40"
    bottomTint: "#0a1230"
    borderColor: Theme.hairlineHi

    Column {
        anchors.fill: parent
        anchors.margins: 28
        spacing: 14

        Row {
            width: parent.width
            spacing: 12
            Text {
                text: "Driver"
                color: Theme.text
                font.family: Theme.familyDisplay
                font.pixelSize: 32
                font.weight: Font.Medium
                anchors.verticalCenter: parent.verticalCenter
            }
            Repeater {
                model: ["voice", "face", "cards", "memory"]
                delegate: Rectangle {
                    required property string modelData
                    height: 52
                    radius: 26
                    width: label.implicitWidth + 40
                    color: root.driver.tab === modelData ? Theme.action : Qt.rgba(1, 1, 1, 0.08)
                    Text {
                        id: label
                        anchors.centerIn: parent
                        text: modelData
                        color: root.driver.tab === modelData ? "#1A1206" : Theme.text
                        font.family: Theme.family
                        font.pixelSize: 20
                        font.capitalization: Font.Capitalize
                    }
                    MouseArea {
                        anchors.fill: parent
                        onClicked: assistant.actionTriggered("driver_tab", ({ "tab": modelData }))
                    }
                }
            }
            Item { width: 40; height: 1 }
            Text {
                text: "Close"
                color: Theme.textDim
                font.family: Theme.family
                font.pixelSize: 20
                anchors.verticalCenter: parent.verticalCenter
                MouseArea {
                    anchors.fill: parent
                    anchors.margins: -12
                    onClicked: {
                        assistant.actionTriggered("driver_close", ({}))
                        root.closed()
                    }
                }
            }
        }

        Text {
            width: parent.width
            visible: root.notice !== ""
            text: root.notice
            wrapMode: Text.WordWrap
            color: Theme.alert
            font.family: Theme.family
            font.pixelSize: Theme.fsCaption + 2
        }

        Item {
            width: parent.width
            height: parent.height - 70 - (root.notice !== "" ? 40 : 0)
            EnrollVoice {
                anchors.fill: parent; visible: root.driver.tab === "voice"
                voice: root.sheetState.voice || ({})
                consent: !!(root.sheetState.consent && root.sheetState.consent.voice)
            }
            EnrollFace {
                anchors.fill: parent; visible: root.driver.tab === "face"
                face: root.sheetState.face || ({})
                consent: !!(root.sheetState.consent && root.sheetState.consent.face)
            }
            CardsSheet { anchors.fill: parent; visible: root.driver.tab === "cards"; cards: root.sheetState.cards || [] }
            MemorySheet { anchors.fill: parent; visible: root.driver.tab === "memory"; memory: root.sheetState.memory || ({}) }
        }
    }

    // Confirmation step for anything destructive or identity-linking. The server holds the request and
    // only acts on Confirm.
    Rectangle {
        anchors.fill: parent
        radius: root.radius
        color: Qt.rgba(0.02, 0.03, 0.08, 0.86)
        visible: root.pending !== null
        z: 10
        MouseArea { anchors.fill: parent }
        Rectangle {
            id: dialog
            width: Math.min(parent.width - 120, 880)
            height: dialogCol.implicitHeight + 64
            anchors.centerIn: parent
            radius: Theme.radiusCard
            color: "#101a3e"
            border.width: 1.5
            border.color: Theme.hairlineHi
            Column {
                id: dialogCol
                x: 32; y: 32
                width: parent.width - 64
                spacing: 28
                Text {
                    width: parent.width
                    wrapMode: Text.WordWrap
                    text: root.pending ? (root.pending.text || "Are you sure?") : ""
                    color: Theme.text
                    font.family: Theme.family
                    font.pixelSize: Theme.fsLead
                }
                Row {
                    spacing: 16
                    PillButton { text: "Cancel"; onClicked: assistant.actionTriggered("driver_cancel", ({})) }
                    PillButton { text: "Confirm"; primary: true; onClicked: assistant.actionTriggered("driver_confirm", ({})) }
                }
            }
        }
    }
}
