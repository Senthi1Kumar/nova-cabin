import QtQuick

// Live Face ID frames from the DriveOS preview hub. Hidden unless this checkout step is a face scan.
Image {
    id: cam
    asynchronous: false
    cache: false
    fillMode: Image.PreserveAspectCrop
    source: visible ? "image://facepreview/frame" : ""
    Timer {
        interval: 66
        running: cam.visible
        repeat: true
        onTriggered: cam.source = "image://facepreview/" + Date.now()
    }
}
