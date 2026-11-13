import QtQuick
import ".."

// Crisp vector icon rasterised at device resolution by the Python provider.
Image {
    id: root
    property string name: "home"
    property color color: Theme.iconInk
    property real stroke: 1.6
    property int size: 32
    width: size; height: size
    sourceSize: Qt.size(size * 2, size * 2)
    smooth: true
    mipmap: true
    asynchronous: false
    fillMode: Image.PreserveAspectFit
    source: Theme.icon(name, color, stroke)
}
