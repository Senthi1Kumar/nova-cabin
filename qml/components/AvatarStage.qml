import QtQuick
import ".."

// Hosts the digital human: ambient key light, breathing body, live face rig.
Item {
    id: root
    property var rig
    property string mode: "idle"
    property real level: 0
    property real energy: 0.2
    property real visemeJaw: 0
    property real visemeRound: 0
    property bool visemesLive: false

    // ambient key light behind the head (breathes with the voice)
    Image { source: Theme.asset("amb_blue.png")
            width: 980; height: 980; x: -150; y: -170; opacity: 0.50 + root.energy * 0.12 }
    Image { source: Theme.asset("amb_violet.png")
            width: 620; height: 620; x: 180; y: 300; opacity: 0.28 + root.energy * 0.25 }

    Item {
        id: body
        width: 610; height: 784
        x: 210; y: 18
        transformOrigin: Item.Bottom

        // breathing: ~14 breaths/min, slightly deeper while listening
        SequentialAnimation on scale { loops: Animation.Infinite
            NumberAnimation { to: root.mode === "listening" ? 1.008 : 1.005; duration: 1900; easing.type: Easing.InOutSine }
            NumberAnimation { to: 1.000; duration: 2400; easing.type: Easing.InOutSine } }

        AvatarRig {
            anchors.fill: parent
            rig: root.rig
            mode: root.mode
            level: root.level
            visemeJaw: root.visemeJaw
            visemeRound: root.visemeRound
            visemesLive: root.visemesLive
        }
    }
}
