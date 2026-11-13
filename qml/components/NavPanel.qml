import QtQuick
import ".."

// Navigation view: large route map with a trip strip. Replaces the
// conversation column while active; the avatar stays.
GlassPanel {
    id: root
    property var nav: ({})
    signal closed()
    height: 760

    RoundedImage {
        id: map
        x: 20; y: 20; width: parent.width - 40; height: parent.height - 156
        radius: Theme.radiusInset
        source: root.nav.map || ""
        overlayBottom: 0.35
        Rectangle { anchors.fill: parent; radius: Theme.radiusInset; color: "transparent"
                    border.width: 1; border.color: Qt.rgba(1, 1, 1, 0.12) }
        MapSurface { anchors.fill: parent; visible: assistant.mapLive }

        // next manoeuvre chip
        Rectangle {
            visible: !!root.nav.name && root.nav.name !== "Current location" && root.nav.active !== false
            x: 22; y: 22; height: 84; radius: 22
            width: Math.min(dest.implicitWidth + 130, map.width - 44)
            color: Qt.rgba(0.04, 0.06, 0.16, 0.82); border.width: 1; border.color: Theme.hairlineHi
            Rectangle { x: 14; anchors.verticalCenter: parent.verticalCenter; width: 56; height: 56; radius: 16
                        color: Theme.action
                        Icon { anchors.centerIn: parent; name: "arrow_up"; size: 34; stroke: 2.2; color: "#1A1206" } }
            Column {
                x: 88; anchors.verticalCenter: parent.verticalCenter
                Text { text: "Heading to"; color: Theme.textDim; font.family: Theme.family; font.pixelSize: 18 }
                Text { id: dest; text: root.nav.name || ""; color: Theme.text; elide: Text.ElideRight
                       width: Math.min(implicitWidth, map.width - 160)
                       font.family: Theme.family; font.pixelSize: 26; font.weight: Font.Medium }
            }
        }
    }

    Row {
        x: 44; anchors.bottom: parent.bottom; anchors.bottomMargin: 34
        spacing: 56
        Repeater {
            model: [ { k: "Arrive", v: root.nav.eta || "—" },
                     { k: "Time",   v: root.nav.duration || "—" },
                     { k: "Distance", v: root.nav.distance || "—" },
                     { k: "Battery at arrival", v: root.nav.arrival || "—" } ]
            Column {
                spacing: 2
                Text { text: modelData.k; color: Theme.textDim; font.family: Theme.family; font.pixelSize: 18 }
                Text { text: modelData.v; color: index === 0 ? "#FFD3A3" : Theme.text
                       font.family: Theme.familyDisplay; font.pixelSize: 34; font.features: { "tnum": 1 } }
            }
        }
    }
    PillButton {
        anchors.right: parent.right; anchors.rightMargin: 28
        anchors.bottom: parent.bottom; anchors.bottomMargin: 34
        width: 200
        text: "End Route"; icon: "close"
        onClicked: root.closed()
    }
}
