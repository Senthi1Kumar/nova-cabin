import QtQuick
import QtQuick.Layouts
import ".."

// Assistant response. The headline streams as the model speaks; the rich
// block (hero map/image/weather, fact rows, source chips, actions) is built
// from whatever the tools returned — see cabin/tools.py for the card schema.
GlassPanel {
    id: root
    property string headline
    property bool streaming: false
    property var card: ({})
    signal actionTriggered(string key)
    function flash(key) {
        for (let i = 0; i < actionRep.count; ++i)
            if (root.actions[i].key === key) actionRep.itemAt(i).flash()
    }

    readonly property var hero: card && card.hero ? card.hero : null
    readonly property var rows: card && card.rows ? card.rows : []
    readonly property var actions: card && card.actions ? card.actions : []
    readonly property var sources: card && card.sources ? card.sources : []
    readonly property bool rich: rows.length > 0 || hero !== null

    implicitHeight: col.implicitHeight + 52
    Behavior on implicitHeight { NumberAnimation { duration: Theme.durSlow; easing.type: Easing.OutCubic } }
    clip: true

    ColumnLayout {
        id: col
        anchors { left: parent.left; right: parent.right; top: parent.top
                  leftMargin: 32; rightMargin: 28; topMargin: 26 }
        spacing: 0

        RowLayout {
            spacing: 22
            Icon { name: "pulse"; size: 50; stroke: 1.8; color: "#9FA8FF"
                   Layout.preferredWidth: 50; Layout.preferredHeight: 50; Layout.leftMargin: -6
                   Layout.alignment: Qt.AlignTop }
            Text {
                Layout.fillWidth: true
                Layout.topMargin: 7
                text: root.headline + (root.streaming ? "<font color='" + Theme.accent + "'>&nbsp;▍</font>" : "")
                textFormat: Text.StyledText
                color: Theme.text; wrapMode: Text.Wrap; lineHeight: 1.15
                maximumLineCount: 4; elide: Text.ElideRight
                font.family: Theme.family; font.pixelSize: Theme.fsLead
            }
        }

        // ── rich block ─────────────────────────────────────────
        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: root.rich ? richCol.implicitHeight + 24 : 0
            Behavior on Layout.preferredHeight { NumberAnimation { duration: Theme.durSlow; easing.type: Easing.OutCubic } }
            clip: true
            opacity: root.rich ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: Theme.durSlow } }

            ColumnLayout {
                id: richCol
                y: 24
                width: parent.width
                spacing: 22

                RowLayout {
                    Layout.leftMargin: 66
                    spacing: 22

                    // hero: map / photo / weather
                    Item {
                        visible: root.hero !== null
                        Layout.preferredWidth: 392; Layout.preferredHeight: Math.max(196, rowsPanel.implicitHeight)

                        RoundedImage {
                            anchors.fill: parent
                            visible: root.hero && root.hero.kind !== "weather" && root.hero.kind !== "face"
                            radius: Theme.radiusInset
                            source: root.hero && root.hero.source ? root.hero.source : ""
                            overlayBottom: 0.45
                        }
                        MapSurface {
                            anchors.fill: parent
                            visible: root.hero && root.hero.kind === "map" && assistant.mapLive && !assistant.mapOpen
                        }
                        FaceSurface {
                            anchors.fill: parent
                            visible: root.hero && root.hero.kind === "face"
                        }
                        Rectangle {
                            anchors.fill: parent
                            visible: root.hero && root.hero.kind === "face"
                            radius: Theme.radiusInset
                            color: "transparent"
                            border.width: 3
                            border.color: !root.hero ? "#FFFFFF"
                                          : root.hero.state === "verified" ? "#8EF0C4"
                                          : root.hero.state === "failed" ? "#FF6B6B" : "#FFFFFF"
                        }
                        Rectangle {          // weather hero
                            anchors.fill: parent
                            visible: root.hero && root.hero.kind === "weather"
                            radius: Theme.radiusInset
                            gradient: Gradient {
                                GradientStop { position: 0; color: "#2B3C86" }
                                GradientStop { position: 1; color: "#141C46" }
                            }
                            Image { source: Theme.asset("amb_amber.png"); width: 300; height: 300; x: 170; y: -150; opacity: 0.55 }
                            Column {
                                x: 26; anchors.verticalCenter: parent.verticalCenter; spacing: 2
                                Text { text: root.hero ? (root.hero.temp || "") : ""; color: Theme.text
                                       font.family: Theme.familyDisplay; font.pixelSize: 76; font.weight: Font.Light }
                                Text { text: root.hero ? (root.hero.cond || "") : ""; color: Theme.text
                                       font.family: Theme.family; font.pixelSize: 24 }
                                Text { text: root.hero ? (root.hero.detail || "") : ""; color: Theme.textDim
                                       font.family: Theme.family; font.pixelSize: 18; topPadding: 6 }
                            }
                        }
                        Rectangle {          // place label pill
                            visible: root.hero && !!root.hero.label && root.hero.kind !== "weather"
                            x: 14; anchors.bottom: parent.bottom; anchors.bottomMargin: 14
                            height: 36; radius: 18
                            width: Math.min(lbl.implicitWidth + 44, parent.width - 28)
                            color: Qt.rgba(0.04, 0.06, 0.16, 0.72)
                            border.width: 1; border.color: Qt.rgba(1, 1, 1, 0.14)
                            Rectangle { width: 8; height: 8; radius: 4; color: Theme.action; x: 15
                                        anchors.verticalCenter: parent.verticalCenter }
                            Text { id: lbl; x: 31; width: parent.width - 44; anchors.verticalCenter: parent.verticalCenter
                                   text: root.hero ? (root.hero.label || "") : ""; elide: Text.ElideRight
                                   color: Theme.text; font.family: Theme.family; font.pixelSize: 18; font.weight: Font.Medium }
                        }
                        Rectangle { anchors.fill: parent; radius: Theme.radiusInset; color: "transparent"
                                    border.width: 1; border.color: Qt.rgba(1, 1, 1, 0.12) }
                    }

                    Rectangle {
                        id: rowsPanel
                        visible: root.rows.length > 0
                        Layout.fillWidth: true
                        Layout.preferredHeight: root.hero ? Math.max(196, implicitHeight) : implicitHeight
                        implicitHeight: rowsCol.implicitHeight + 12
                        radius: Theme.radiusInset
                        color: Theme.glassInset
                        border.width: 1; border.color: Theme.hairline
                        Column {
                            id: rowsCol
                            anchors.left: parent.left; anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            Repeater {
                                model: root.rows.slice(0, 4)
                                InfoRow {
                                    width: rowsCol.width; height: 46
                                    icon: modelData.icon || "sparkle"; label: modelData.label || ""
                                    value: modelData.value || ""
                                    iconColor: modelData.tint || Theme.iconInk
                                    divider: index < Math.min(root.rows.length, 4) - 1
                                    opacity: root.rich ? 1 : 0
                                    Behavior on opacity { SequentialAnimation {
                                        PauseAnimation { duration: root.rich ? 160 + index * 80 : 0 }
                                        NumberAnimation { duration: Theme.durMed } } }
                                }
                            }
                        }
                    }
                }

                Flow {       // sources
                    visible: root.sources.length > 0
                    Layout.leftMargin: 66; Layout.fillWidth: true
                    spacing: 10
                    Text { text: "Sources"; color: Theme.textFaint; font.family: Theme.family; font.pixelSize: 17
                           height: 32; verticalAlignment: Text.AlignVCenter; rightPadding: 4 }
                    Repeater {
                        model: root.sources
                        Rectangle {
                            height: 32; radius: 16; width: st.implicitWidth + 28
                            color: Qt.rgba(0.55, 0.63, 1.0, 0.08); border.width: 1; border.color: Theme.hairline
                            Text { id: st; anchors.centerIn: parent; text: modelData; color: Theme.textDim
                                   font.family: Theme.family; font.pixelSize: 16 }
                        }
                    }
                }

                RowLayout {
                    visible: root.actions.length > 0
                    Layout.leftMargin: 66
                    spacing: 18
                    Repeater {
                        id: actionRep
                        model: root.actions
                        PillButton {
                            Layout.fillWidth: true
                            text: modelData.text; icon: modelData.icon || ""; primary: !!modelData.primary
                            onClicked: root.actionTriggered(modelData.key)
                        }
                    }
                }
            }
        }
    }
}
