import QtQuick
import ".."

Item {
    id: root
    property string driverName: "Driver"
    property url driverPhoto: Theme.asset("driver.png")
    property string clock: Qt.formatTime(new Date(), "h:mm AP")
    property string outsideTemp: "72°F"
    property bool simulation: false
    property var tour: ({})
    property string network: "5G"
    property int signalBars: 4
    property bool bluetooth: true
    property bool wifi: true
    property var notice: ({})
    property string noticeCode: ""
    property bool noticeOpen: false
    readonly property bool hasNotice: noticeCode !== ""
    onHasNoticeChanged: noticeOpen = hasNotice
    height: Theme.statusH

    Timer { interval: 1000; running: true; repeat: true; triggeredOnStart: true
            onTriggered: root.clock = Qt.formatTime(new Date(), "h:mm AP") }

    // profile
    Row {
        x: 48; anchors.verticalCenter: parent.verticalCenter; spacing: 18
        Item {
            width: 52; height: 52
            Rectangle { anchors.fill: parent; radius: width / 2; color: "transparent"
                        border.width: 1.5; border.color: Qt.rgba(0.85, 0.9, 1, 0.55) }
            RoundedImage { anchors.fill: parent; anchors.margins: 3; radius: width / 2; source: root.driverPhoto }
            MouseArea { anchors.fill: parent; onClicked: assistant.actionTriggered("driver_open", ({})) }
        }
        Text { text: root.driverName; color: Theme.text; font.family: Theme.family
               font.pixelSize: Theme.fsStatus; anchors.verticalCenter: parent.verticalCenter }
        Rectangle {          // simulation badge (+ tour progress)
            visible: root.simulation
            anchors.verticalCenter: parent.verticalCenter
            height: 34; radius: 17; width: simRow.implicitWidth + 30
            color: Qt.rgba(1.0, 0.70, 0.40, 0.12); border.width: 1; border.color: Qt.rgba(1.0, 0.74, 0.45, 0.55)
            Row {
                id: simRow; anchors.centerIn: parent; spacing: 10
                Rectangle { width: 8; height: 8; radius: 4; color: Theme.action; anchors.verticalCenter: parent.verticalCenter
                            SequentialAnimation on opacity { running: !!root.tour.running; loops: Animation.Infinite
                                NumberAnimation { to: 0.25; duration: 700 } NumberAnimation { to: 1; duration: 700 } } }
                Text {
                    text: "SIMULATION" + (root.tour.running ? "  ·  TOUR " + root.tour.step + "/" + root.tour.total : "")
                    color: "#FFD3A3"; font.family: Theme.family; font.pixelSize: 16; font.weight: Font.Medium
                    font.letterSpacing: 1.2
                }
            }
        }
    }

    Text {
        anchors.centerIn: parent
        text: root.clock; color: Theme.text
        font.family: Theme.family; font.pixelSize: 30; font.weight: Font.Medium
        font.features: { "tnum": 1 }
    }

    Row {
        anchors.right: parent.right; anchors.rightMargin: 44
        anchors.verticalCenter: parent.verticalCenter
        spacing: 34
        Text { visible: root.outsideTemp !== ""; text: root.outsideTemp; color: Theme.text; font.family: Theme.family
               font.pixelSize: Theme.fsStatus; anchors.verticalCenter: parent.verticalCenter }
        Item { width: 4; height: 1 }
        Item {   // cellular bars with dimmed inactive bars
            width: 34; height: 30; anchors.verticalCenter: parent.verticalCenter
            Repeater {
                model: 4
                Rectangle {
                    width: 5; radius: 1.5
                    height: 9 + index * 6
                    x: index * 8; y: parent.height - height - 2
                    color: index < root.signalBars ? Theme.text : Qt.rgba(1, 1, 1, 0.22)
                }
            }
        }
        Text { text: root.network; color: Theme.text; font.family: Theme.family; font.weight: Font.Medium
               font.pixelSize: 25; anchors.verticalCenter: parent.verticalCenter }
        Icon { name: "bluetooth"; size: 30; anchors.verticalCenter: parent.verticalCenter
               opacity: root.bluetooth ? 1 : 0.3 }
        Icon { name: "wifi"; size: 32; anchors.verticalCenter: parent.verticalCenter
               opacity: root.wifi ? 1 : 0.3 }
        Item {
            id: bell
            width: 44; height: 44; anchors.verticalCenter: parent.verticalCenter
            Icon { name: "bell"; size: 32; anchors.centerIn: parent }
            Rectangle { visible: root.hasNotice; width: 9; height: 9; radius: 4.5
                        color: Theme.action; x: 28; y: 6
                        border.width: 2; border.color: Theme.bgTop }
            MouseArea {
                anchors.fill: parent
                enabled: root.hasNotice
                onClicked: root.noticeOpen = !root.noticeOpen
            }
        }
    }

    Rectangle {
        visible: root.noticeOpen && root.hasNotice
        z: 60
        width: 360; height: 118; radius: 22
        anchors.right: parent.right; anchors.rightMargin: 36
        anchors.top: parent.bottom; anchors.topMargin: 8
        color: "#1c2028"
        border.width: 1; border.color: Qt.rgba(1, 1, 1, 0.16)
        Rectangle {
            x: 16; y: 18; width: 34; height: 34; radius: 10
            color: Theme.action
            Icon { anchors.centerIn: parent; name: "bell"; size: 18; color: "#14182A" }
        }
        Column {
            x: 64; y: 16; width: parent.width - 80; spacing: 4
            Text { text: "YOUR PHONE"; color: Theme.textDim; font.family: Theme.family
                   font.pixelSize: 12; font.weight: Font.DemiBold; font.letterSpacing: 1.1 }
            Text { text: root.noticeCode; color: Theme.text; font.family: Theme.family
                   font.pixelSize: 28; font.weight: Font.Bold; font.letterSpacing: 3 }
            Text { width: parent.width; elide: Text.ElideRight
                   text: root.notice.purpose ? ("To approve " + root.notice.purpose) : "Checkout code"
                   color: Theme.textDim; font.family: Theme.family; font.pixelSize: 14 }
        }
    }

    // luminous base line
    Rectangle {
        anchors.bottom: parent.bottom; width: parent.width; height: 1
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.0; color: "transparent" }
            GradientStop { position: 0.18; color: Qt.rgba(0.40, 0.55, 1.0, 0.55) }
            GradientStop { position: 0.5; color: Qt.rgba(0.40, 0.55, 1.0, 0.18) }
            GradientStop { position: 0.85; color: Qt.rgba(0.40, 0.55, 1.0, 0.08) }
            GradientStop { position: 1.0; color: "transparent" }
        }
    }
}
