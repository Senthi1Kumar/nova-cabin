import QtQuick
import ".."

// Diagnostics: temperature, load, memory, last-turn latency and device state.
// Bound to the `diagnostics` context property (cabin/telemetry/qt.py).
GlassPanel {
    id: root
    width: 330; height: col.implicitHeight + 28
    radius: 12
    readonly property var d: (typeof diagnostics !== "undefined" && diagnostics) ? diagnostics.info : ({})
    readonly property var lat: d.latency || ({})
    readonly property var dev: d.devices || ({})
    readonly property var tel: d.telemetry || ({})
    function num(v, digits, unit) { return (v === undefined || v === null) ? "—" : Number(v).toFixed(digits) + unit }
    function str(v) { return (v === undefined || v === null || v === "") ? "—" : String(v) }

    component Row2: Item {
        property string label
        property string value
        property color tint: Theme.text
        width: parent.width; height: 24
        Text { text: parent.label; color: Theme.textDim; font.family: Theme.family; font.pixelSize: 16
               anchors.verticalCenter: parent.verticalCenter }
        Text { text: parent.value; color: parent.tint; font.family: Theme.family; font.pixelSize: 16
               font.features: { "tnum": 1 }; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter }
    }

    Column {
        id: col
        x: 16; y: 14; width: parent.width - 32; spacing: 2
        Text { text: "Diagnostics"; color: Theme.accentHi; font.family: Theme.family; font.pixelSize: 18
               font.weight: Font.DemiBold; bottomPadding: 6 }
        Row2 { label: "Temperature"; value: root.num(root.d.temp_c, 1, " °C")
               tint: (root.d.temp_c || 0) >= 75 ? Theme.alert : (root.d.temp_c || 0) >= 65 ? Theme.action : Theme.text }
        Row2 { label: "Throttled"; value: root.d.throttled === true ? "yes" : root.d.throttled === false ? "no" : "—"
               tint: root.d.throttled === true ? Theme.alert : Theme.text }
        Row2 { label: "CPU · clock"; value: root.num(root.d.cpu, 0, " %") + " · " + root.num(root.d.arm_mhz, 0, " MHz") }
        Row2 { label: "Memory"; value: root.num(root.d.rss_mb, 0, " MB") }
        Row2 { label: "NPU"; value: root.d.npu_util != null ? root.num(root.d.npu_util, 0, " %") : root.str(root.d.npu) }
        Row2 { id: fpsRow; label: "UI"; value: perfFps.toFixed(0) + " fps" }
        Rectangle { width: parent.width; height: 1; color: Theme.hairline }
        Row2 { label: "STT"; value: root.num(root.lat.stt_ms, 0, " ms") }
        Row2 { label: "First token"; value: root.num(root.lat.first_token_ms, 0, " ms") }
        Row2 { label: "Tools"; value: root.num(root.lat.tools_ms, 0, " ms") }
        Row2 { label: "First audio"; value: root.num(root.lat.ttfa_ms, 0, " ms") }
        Rectangle { width: parent.width; height: 1; color: Theme.hairline }
        Row2 { label: "Mic"; value: root.str(root.dev.mic) }
        Row2 { label: "AEC"; value: root.str(root.dev.aec) }
        Row2 { label: "GPS"; value: root.str(root.dev.gps) }
        Row2 { label: "Logging"; value: root.tel.enabled ? "on · " + (root.tel.dropped || 0) + " dropped" : "off"
               tint: root.tel.enabled ? Theme.ok : Theme.textFaint }
    }

    property real perfFps: 0
    property int _frames: 0
    property real _acc: 0
    FrameAnimation {
        running: root.visible
        onTriggered: {
            root._frames++; root._acc += frameTime
            if (root._acc >= 0.5) { root.perfFps = root._frames / root._acc; root._frames = 0; root._acc = 0
                                  if (typeof diagnostics !== "undefined" && diagnostics) diagnostics.reportFps(root.perfFps) }
        }
    }
}
