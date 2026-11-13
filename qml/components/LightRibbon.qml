import QtQuick
import ".."

// Luminous horizon. GPU path: one fragment shader (shaders/ribbon.frag) driven
// by a vsync-locked FrameAnimation — no CPU rasterisation, no offscreen blur.
// Software scenegraph (no shaders) falls back to a static Canvas rendering.
Item {
    id: root
    property real energy: 0.3          // 0 idle … 1 speaking (already smoothed)
    property bool animate: true

    readonly property bool gpu: GraphicsInfo.api !== GraphicsInfo.Software
                                && GraphicsInfo.api !== GraphicsInfo.Unknown

    ShaderEffect {
        id: fx
        visible: root.gpu
        anchors.fill: parent
        blending: true
        property real time: 0
        property real energy: root.energy
        property size res: Qt.size(width, height)
        fragmentShader: Qt.resolvedUrl("../shaders/ribbon.frag.qsb")
    }
    FrameAnimation {
        running: root.gpu && root.animate && root.visible
        // speed up the drift a little while the assistant talks
        onTriggered: fx.time += frameTime * (1.0 + root.energy * 0.8)
    }

    Loader {
        anchors.fill: parent
        active: !root.gpu
        sourceComponent: LightRibbonCanvas { animate: false; energy: 0.3; cheap: true }   // static: painted once
    }
}
