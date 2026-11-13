import QtQuick
import ".."

Item {
    id: root
    property var cards: []
    property string editing: ""
    property string brand: "Visa"

    Flickable {
        anchors.fill: parent
        contentHeight: col.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        Column {
            id: col
            width: parent.width
            spacing: 14
            Text { text: "Cards"; color: Theme.text; font.family: Theme.family; font.pixelSize: Theme.fsLead }
            Text {
                width: parent.width
                wrapMode: Text.WordWrap
                color: Theme.textDim
                font.family: Theme.family
                font.pixelSize: Theme.fsCaption + 2
                text: "Brand, last four digits, expiry and a label. A full card number is never stored."
            }
            Repeater {
                model: root.cards
                delegate: Rectangle {
                    id: row
                    required property var modelData
                    readonly property bool isEditing: root.editing === modelData.token
                    width: col.width
                    height: 92
                    radius: Theme.radiusInset
                    color: Qt.rgba(1, 1, 1, 0.05)
                    border.width: 1
                    border.color: modelData.default ? Theme.action : Theme.hairline
                    Row {
                        anchors.fill: parent
                        anchors.margins: 14
                        spacing: 12
                        Column {
                            width: 520
                            visible: !row.isEditing
                            anchors.verticalCenter: parent.verticalCenter
                            Text {
                                width: parent.width
                                elide: Text.ElideRight
                                text: modelData.label
                                color: Theme.text
                                font.family: Theme.family
                                font.pixelSize: Theme.fsBody
                            }
                            Text {
                                text: modelData.brand + " ••" + modelData.last4 + "  ·  expires " + modelData.expiry + (modelData.default ? "  ·  default" : "")
                                color: modelData.default ? Theme.action : Theme.textFaint
                                font.family: Theme.family
                                font.pixelSize: Theme.fsCaption
                            }
                        }
                        DriverField {
                            id: labelField
                            visible: row.isEditing
                            width: 330
                            maximumLength: 40
                            placeholder: "Label"
                            text: modelData.label
                            anchors.verticalCenter: parent.verticalCenter
                            onAccepted: saveBtn.clicked()
                        }
                        DriverField {
                            id: expField
                            visible: row.isEditing
                            width: 150
                            maximumLength: 5
                            placeholder: "MM/YY"
                            text: modelData.expiry
                            anchors.verticalCenter: parent.verticalCenter
                            onAccepted: saveBtn.clicked()
                        }
                        PillButton {
                            visible: !row.isEditing && !modelData.default
                            text: "Default"
                            anchors.verticalCenter: parent.verticalCenter
                            onClicked: assistant.actionTriggered("driver_card_default", ({ "token": modelData.token }))
                        }
                        PillButton {
                            visible: !row.isEditing
                            text: "Edit"
                            anchors.verticalCenter: parent.verticalCenter
                            onClicked: { root.editing = modelData.token; labelField.focusInput() }
                        }
                        PillButton {
                            visible: !row.isEditing
                            text: "Remove"
                            anchors.verticalCenter: parent.verticalCenter
                            onClicked: assistant.actionTriggered("driver_card_remove", ({ "token": modelData.token }))
                        }
                        PillButton {
                            id: saveBtn
                            visible: row.isEditing
                            text: "Save"
                            primary: true
                            anchors.verticalCenter: parent.verticalCenter
                            onClicked: {
                                assistant.actionTriggered("driver_card_edit", ({ "token": modelData.token, "label": labelField.text, "expiry": expField.text }))
                                root.editing = ""
                            }
                        }
                        PillButton {
                            visible: row.isEditing
                            text: "Cancel"
                            anchors.verticalCenter: parent.verticalCenter
                            onClicked: root.editing = ""
                        }
                    }
                }
            }

            Text { text: "Add a card"; color: Theme.text; font.family: Theme.family; font.pixelSize: Theme.fsBody; topPadding: 8 }
            Row {
                spacing: 10
                Repeater {
                    model: ["Visa", "Mastercard", "Amex"]
                    delegate: Rectangle {
                        required property string modelData
                        height: 52
                        radius: 26
                        width: brandLabel.implicitWidth + 40
                        color: root.brand === modelData ? Theme.action : Qt.rgba(1, 1, 1, 0.08)
                        Text {
                            id: brandLabel
                            anchors.centerIn: parent
                            text: modelData
                            color: root.brand === modelData ? "#1A1206" : Theme.text
                            font.family: Theme.family
                            font.pixelSize: 20
                        }
                        MouseArea { anchors.fill: parent; onClicked: root.brand = modelData }
                    }
                }
            }
            Row {
                spacing: 12
                DriverField { id: addLabel; width: 300; placeholder: "Label (optional)"; maximumLength: 40 }
                DriverField {
                    id: addLast4
                    width: 230
                    placeholder: "Last 4 digits"
                    maximumLength: 4
                    inputMethodHints: Qt.ImhDigitsOnly
                    validator: RegularExpressionValidator { regularExpression: /^\d{0,4}$/ }
                }
                DriverField { id: addExpiry; width: 190; placeholder: "MM/YY"; maximumLength: 5 }
                PillButton {
                    text: "Add card"
                    primary: true
                    enabled: addLast4.text.length === 4 && addExpiry.text.length === 5
                    opacity: enabled ? 1 : 0.4
                    onClicked: {
                        assistant.actionTriggered("driver_card_add", ({ "label": addLabel.text, "brand": root.brand, "last4": addLast4.text, "expiry": addExpiry.text }))
                        addLabel.text = ""; addLast4.text = ""; addExpiry.text = ""
                    }
                }
            }
        }
    }
}
