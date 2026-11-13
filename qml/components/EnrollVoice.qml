import QtQuick
import ".."

Item {
    id: root
    property var voice: ({})
    property bool consent: false
    readonly property string stage: root.voice.stage || "idle"
    readonly property bool enrolling: stage === "ready" || stage === "recording" || stage === "analyzing"
    readonly property bool enrolled: !!root.voice.enrolled && !enrolling
    readonly property var last: root.voice.last || ({})

    Column {
        anchors.fill: parent
        spacing: 18
        Text { text: "Voice ID"; color: Theme.text; font.family: Theme.family; font.pixelSize: Theme.fsLead }

        // not enrolled (or starting over)
        Column {
            width: parent.width
            spacing: 18
            visible: !root.enrolling
            Text {
                width: parent.width
                wrapMode: Text.WordWrap
                color: root.stage === "failed" ? Theme.alert : Theme.textDim
                font.family: Theme.family
                font.pixelSize: Theme.fsBody
                text: root.voice.message || (root.enrolled
                      ? "Voice ID is on. It is used to approve payments."
                      : "Read " + (root.voice.of || 3) + " short phrases aloud. A noisy or quiet take is rejected and you try again.")
            }
            Text {
                visible: root.enrolled
                color: Theme.text
                font.family: Theme.family
                font.pixelSize: Theme.fsBody
                text: "Enrolled as " + (root.voice.name || "Driver")
            }
            DriverToggle {
                width: parent.width
                text: "I agree to Nova recording my voice to build a Voice ID, stored encrypted on this device."
                checked: root.consent
                onToggled: (value) => assistant.actionTriggered("driver_consent", ({ "kind": "voice", "value": value }))
            }
            Row {
                spacing: 14
                DriverField {
                    id: nameField
                    width: 380
                    placeholder: "Your name"
                    maximumLength: 32
                    text: root.voice.name || "Driver"
                    onAccepted: root.enrolled ? renameBtn.clicked() : startBtn.clicked()
                }
                PillButton {
                    id: startBtn
                    text: root.enrolled ? "Re-enroll" : "Start"
                    primary: true
                    enabled: root.consent && nameField.text.trim() !== ""
                    opacity: enabled ? 1 : 0.4
                    onClicked: assistant.actionTriggered("driver_voice_start", ({ "name": nameField.text }))
                }
                PillButton {
                    id: renameBtn
                    visible: root.enrolled
                    text: "Rename"
                    enabled: nameField.text.trim() !== ""
                    opacity: enabled ? 1 : 0.4
                    onClicked: assistant.actionTriggered("driver_voice_rename", ({ "name": nameField.text }))
                }
                PillButton {
                    visible: root.enrolled
                    text: "Delete"
                    onClicked: assistant.actionTriggered("driver_voice_delete", ({}))
                }
            }
        }

        // prompted takes
        Column {
            width: parent.width
            spacing: 18
            visible: root.enrolling
            Row {
                spacing: 12
                Repeater {
                    model: root.voice.of || 3
                    delegate: Rectangle {
                        required property int index
                        width: 56; height: 12; radius: 6
                        color: index < (root.voice.kept || 0) ? Theme.ok
                             : index === (root.voice.kept || 0) ? Theme.action : Qt.rgba(1, 1, 1, 0.14)
                    }
                }
            }
            Text {
                color: Theme.textDim
                font.family: Theme.family
                font.pixelSize: Theme.fsBody
                text: "Take " + Math.min(root.voice.step || 1, root.voice.of || 3) + " of " + (root.voice.of || 3)
            }
            Text {
                width: parent.width
                wrapMode: Text.WordWrap
                color: Theme.text
                font.family: Theme.familyDisplay
                font.pixelSize: 36
                text: "“" + (root.voice.prompt || "") + "”"
            }
            Text {
                width: parent.width
                wrapMode: Text.WordWrap
                color: root.last.ok === false ? Theme.alert : Theme.textDim
                font.family: Theme.family
                font.pixelSize: Theme.fsBody
                text: root.voice.message || ""
            }
            Text {
                visible: root.last.voiced_s !== undefined
                color: Theme.textFaint
                font.family: Theme.family
                font.pixelSize: Theme.fsCaption
                text: "Last take: " + root.last.voiced_s + " s of speech · " + root.last.snr_db + " dB signal to noise"
            }
            Row {
                spacing: 14
                PillButton {
                    text: root.stage === "recording" ? "Listening…" : (root.stage === "analyzing" ? "Checking…" : "Record")
                    primary: root.stage === "ready"
                    enabled: root.stage === "ready"
                    opacity: enabled ? 1 : 0.5
                    onClicked: assistant.actionTriggered("driver_voice_take", ({}))
                }
                PillButton {
                    text: "Cancel"
                    enabled: root.stage === "ready"
                    opacity: enabled ? 1 : 0.5
                    onClicked: assistant.actionTriggered("driver_voice_cancel", ({}))
                }
            }
        }
    }
}
