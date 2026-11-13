import QtQuick
import ".."

// Persistent bottom bar: app launcher (left) + climate (right).
Item {
    id: root
    property int current: 0
    property int cabinTempF: 72
    property int seatHeat: 0
    signal appSelected(int index, string key)
    signal tempNudged(int delta)
    signal seatHeatClicked()
    height: Theme.dockH

    readonly property var apps: [
        { key: "home",  icon: "home"  }, { key: "apps",  icon: "grid"  },
        { key: "media", icon: "music" }, { key: "nav",   icon: "nav"   },
        { key: "car",   icon: "car"   }, { key: "phone", icon: "phone" },
        { key: "camera",icon: "video" }
    ]

    Rectangle {
        anchors.fill: parent
        gradient: Gradient {
            GradientStop { position: 0.0; color: "#0A1022" }
            GradientStop { position: 1.0; color: Theme.dock }
        }
    }
    Rectangle { width: parent.width; height: 1; color: Theme.hairline }

    Row {
        id: apps
        x: 40; height: parent.height
        Repeater {
            model: root.apps
            Item {
                width: 172; height: apps.height
                readonly property bool active: index === root.current
                Icon {
                    anchors.centerIn: parent; size: 46; name: modelData.icon; stroke: 1.5
                    color: active ? "#FFC58A" : Theme.iconInk
                    opacity: active ? 1 : 0.86
                }
                // active indicator: amber filament + bloom
                Image { visible: active; source: Theme.asset("beam_amber.png")
                        width: 150; height: 40; anchors.horizontalCenter: parent.horizontalCenter
                        y: parent.height - 28; opacity: 0.9 }
                Rectangle { visible: active; width: 64; height: 3; radius: 1.5
                            anchors.horizontalCenter: parent.horizontalCenter
                            y: parent.height - 10; color: Theme.action }
                MouseArea { anchors.fill: parent
                            onClicked: { root.current = index; root.appSelected(index, modelData.key) } }
            }
        }
    }

    Rectangle {
        x: apps.x + apps.width + 34; anchors.verticalCenter: parent.verticalCenter
        width: 1; height: 44; color: Theme.hairlineHi
    }

    Row {
        anchors.right: parent.right; anchors.rightMargin: 56
        height: parent.height; spacing: 46
        Icon { name: "seat"; size: 44; stroke: 1.5; anchors.verticalCenter: parent.verticalCenter }
        Row {
            spacing: 22; anchors.verticalCenter: parent.verticalCenter
            Icon { name: "chev_l"; size: 34; anchors.verticalCenter: parent.verticalCenter
                   MouseArea { anchors.fill: parent; anchors.margins: -16; onClicked: root.tempNudged(-1) } }
            Text {
                width: 112; horizontalAlignment: Text.AlignHCenter
                anchors.verticalCenter: parent.verticalCenter
                text: root.cabinTempF + "<font size='1'>°F</font>"
                textFormat: Text.StyledText
                color: Theme.text; font.family: Theme.familyDisplay; font.pixelSize: Theme.fsTemp
                font.features: { "tnum": 1 }
            }
            Icon { name: "chev_r"; size: 34; anchors.verticalCenter: parent.verticalCenter
                   MouseArea { anchors.fill: parent; anchors.margins: -16; onClicked: root.tempNudged(1) } }
        }
        Icon { name: "fan"; size: 44; stroke: 1.5; anchors.verticalCenter: parent.verticalCenter }
        Item {
            width: 46; height: 60; anchors.verticalCenter: parent.verticalCenter
            Icon { name: "seat_heat"; size: 46; stroke: 1.5; anchors.centerIn: parent
                   color: root.seatHeat > 0 ? "#FFB25E" : Theme.iconInk }
            Row {   // heat level pips
                anchors.horizontalCenter: parent.horizontalCenter; anchors.bottom: parent.bottom; anchors.bottomMargin: -6
                spacing: 4; visible: root.seatHeat > 0
                Repeater { model: 3
                    Rectangle { width: 8; height: 3; radius: 1.5
                                color: index < root.seatHeat ? Theme.action : Qt.rgba(1, 1, 1, 0.2) } }
            }
            MouseArea { anchors.fill: parent; anchors.margins: -14; onClicked: root.seatHeatClicked() }
        }
    }
}
