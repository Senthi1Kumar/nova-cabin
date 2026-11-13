import QtQuick
import ".."

// Transient system message (errors, confirmations). Queues if several arrive.
Item {
    id: root
    width: pill.width; height: 56
    property var queue: []
    opacity: 0
    function show(msg) { queue.push(msg); if (!timer.running && opacity === 0) next() }
    function next() {
        if (!queue.length) { opacity = 0; return }
        txt.text = queue.shift(); opacity = 1; timer.restart()
    }
    Behavior on opacity { NumberAnimation { duration: Theme.durMed } }
    Timer { id: timer; interval: 3600; onTriggered: { root.opacity = 0; gap.start() } }
    Timer { id: gap; interval: 320; onTriggered: root.next() }

    Rectangle {
        id: pill
        height: parent.height; radius: height / 2
        width: Math.min(txt.implicitWidth + 84, 1100)
        color: Qt.rgba(0.07, 0.09, 0.22, 0.94)
        border.width: 1; border.color: Theme.hairlineHi
        Icon { x: 22; anchors.verticalCenter: parent.verticalCenter; name: "sparkle"; size: 24; color: Theme.action }
        Text { id: txt; x: 58; width: parent.width - 80; anchors.verticalCenter: parent.verticalCenter
               elide: Text.ElideRight; color: Theme.text; font.family: Theme.family; font.pixelSize: 21 }
        MouseArea { anchors.fill: parent; onClicked: { timer.stop(); root.opacity = 0; gap.start() } }
    }
}
