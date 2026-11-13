import QtQuick
import ".."

Item {
    id: root
    property var face: ({})
    property bool consent: false
    readonly property string stage: root.face.stage || ""
    readonly property bool busy: !!root.face.busy
    property string editing: ""

    Flickable {
        anchors.fill: parent
        contentHeight: col.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        visible: root.stage !== "capturing"

        Column {
            id: col
            width: parent.width
            spacing: 16
            Text { text: "Face ID"; color: Theme.text; font.family: Theme.family; font.pixelSize: Theme.fsLead }
            Text {
                width: parent.width
                wrapMode: Text.WordWrap
                color: (root.face.degraded || root.stage === "failed") ? Theme.alert : Theme.textDim
                font.family: Theme.family
                font.pixelSize: Theme.fsBody
                text: root.face.message || "Enroll a face for payments, or manage the saved profiles."
            }

            // a capture or a finished operation
            Row {
                spacing: 14
                visible: root.busy || root.stage === "done" || root.stage === "failed" || root.stage === "link_confirm"
                PillButton {
                    text: root.busy || root.stage === "link_confirm" ? "Cancel" : "OK"
                    onClicked: assistant.actionTriggered("driver_face_dismiss", ({}))
                }
            }

            PillButton {
                visible: !!root.face.degraded
                text: "Check camera"
                onClicked: assistant.actionTriggered("driver_tab", ({ "tab": "face" }))
            }

            Repeater {
                model: root.face.profiles || []
                delegate: Rectangle {
                    id: card
                    required property var modelData
                    readonly property bool isSelected: modelData.id === root.face.selected
                    readonly property bool isEditing: root.editing === modelData.id
                    width: col.width
                    height: 96
                    radius: Theme.radiusInset
                    color: Qt.rgba(1, 1, 1, 0.05)
                    border.width: 1
                    border.color: isSelected ? Theme.action : Theme.hairline

                    Row {
                        anchors.fill: parent
                        anchors.margins: 16
                        spacing: 14
                        Column {
                            width: 300
                            anchors.verticalCenter: parent.verticalCenter
                            visible: !card.isEditing
                            Text {
                                text: modelData.name || "Unnamed"
                                color: Theme.text
                                font.family: Theme.family
                                font.pixelSize: Theme.fsBody
                                elide: Text.ElideRight
                                width: parent.width
                            }
                            Text {
                                text: (card.isSelected ? "In use" : "Saved") + (modelData.linked ? " · linked" : "") + " · " + modelData.role
                                color: card.isSelected ? Theme.ok : Theme.textFaint
                                font.family: Theme.family
                                font.pixelSize: Theme.fsCaption
                            }
                        }
                        DriverField {
                            id: renameField
                            visible: card.isEditing
                            width: 300
                            maximumLength: 32
                            text: modelData.name
                            anchors.verticalCenter: parent.verticalCenter
                            onAccepted: saveBtn.clicked()
                        }
                        PillButton {
                            visible: !card.isEditing && !card.isSelected
                            text: "Use"
                            anchors.verticalCenter: parent.verticalCenter
                            onClicked: assistant.actionTriggered("driver_face_select", ({ "profile_id": modelData.id }))
                        }
                        PillButton {
                            visible: !card.isEditing
                            text: "Rename"
                            anchors.verticalCenter: parent.verticalCenter
                            onClicked: { root.editing = modelData.id; renameField.focusInput() }
                        }
                        PillButton {
                            visible: !card.isEditing
                            text: "Re-enroll"
                            enabled: root.consent && !root.busy
                            opacity: enabled ? 1 : 0.4
                            anchors.verticalCenter: parent.verticalCenter
                            onClicked: assistant.actionTriggered("driver_face_start", ({ "profile_id": modelData.id, "name": modelData.name }))
                        }
                        PillButton {
                            visible: !card.isEditing
                            text: "Delete"
                            anchors.verticalCenter: parent.verticalCenter
                            onClicked: assistant.actionTriggered("driver_face_delete", ({ "profile_id": modelData.id }))
                        }
                        PillButton {
                            id: saveBtn
                            visible: card.isEditing
                            text: "Save"
                            primary: true
                            anchors.verticalCenter: parent.verticalCenter
                            onClicked: {
                                assistant.actionTriggered("driver_face_rename", ({ "profile_id": modelData.id, "name": renameField.text }))
                                root.editing = ""
                            }
                        }
                        PillButton {
                            visible: card.isEditing
                            text: "Cancel"
                            anchors.verticalCenter: parent.verticalCenter
                            onClicked: root.editing = ""
                        }
                    }
                }
            }

            DriverToggle {
                width: parent.width
                text: "I agree to Nova using the face camera to enroll and recognise me. The template stays on the camera."
                checked: root.consent
                onToggled: (value) => assistant.actionTriggered("driver_consent", ({ "kind": "face", "value": value }))
            }
            Row {
                spacing: 14
                DriverField {
                    id: newName
                    width: 380
                    placeholder: "Name for the new profile"
                    maximumLength: 32
                    onAccepted: enrollBtn.clicked()
                }
                PillButton {
                    id: enrollBtn
                    text: "Enroll new face"
                    primary: true
                    enabled: root.consent && !root.busy && !root.face.degraded && !root.face.full && newName.text.trim() !== ""
                    opacity: enabled ? 1 : 0.4
                    onClicked: assistant.actionTriggered("driver_face_start", ({ "name": newName.text }))
                }
            }
        }
    }

    Item {
        anchors.fill: parent
        visible: root.stage === "capturing"
        z: 5
        Column {
            anchors.centerIn: parent
            spacing: 18
            width: 320
            Item {
                width: 280; height: 280
                anchors.horizontalCenter: parent.horizontalCenter
                FaceSurface { anchors.fill: parent }
                Rectangle {
                    anchors.fill: parent
                    radius: 22
                    color: "transparent"
                    border.width: 3
                    border.color: "#FFFFFF"
                }
            }
            Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                text: "Look at the camera"
                color: Theme.text
                font.family: Theme.family
                font.pixelSize: Theme.fsLead
            }
            Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                text: "Keep one face in view until enrollment finishes."
                color: Theme.textDim
                font.family: Theme.family
                font.pixelSize: Theme.fsBody
            }
            PillButton {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "Cancel"
                onClicked: assistant.actionTriggered("driver_face_dismiss", ({}))
            }
        }
    }
}
