pragma Singleton
import QtQuick

// Single source of truth for the cabin HMI visual language.
// Layout is authored on a 1920x1080 reference canvas; Main.qml scales it
// uniformly to the physical panel (1920x720, 2560x1440, 2880x1080 ...).
QtObject {
    // ── surfaces ────────────────────────────────────────────────
    readonly property color bgTop:      "#050913"
    readonly property color bgMid:      "#0a1430"
    readonly property color bgBottom:   "#060a17"
    readonly property color dock:       "#060912"
    readonly property color hairline:   Qt.rgba(0.55, 0.65, 1.0, 0.16)
    readonly property color hairlineHi: Qt.rgba(0.62, 0.72, 1.0, 0.34)

    readonly property color glassTop:    Qt.rgba(0.20, 0.27, 0.56, 0.42)
    readonly property color glassBottom: Qt.rgba(0.08, 0.11, 0.27, 0.48)
    readonly property color glassInset:  Qt.rgba(0.36, 0.45, 0.85, 0.10)

    // ── ink ─────────────────────────────────────────────────────
    readonly property color text:      "#EEF2FF"
    readonly property color textDim:   "#A9B4D6"
    readonly property color textFaint: "#6B7699"
    readonly property color iconInk:   "#DCE3FA"

    // ── accents ─────────────────────────────────────────────────
    readonly property color accent:     "#7C86FF"   // assistant identity (violet-blue)
    readonly property color accentHi:   "#B69CFF"
    readonly property color accentCyan: "#4CD6FF"
    readonly property color action:     "#FFB25E"   // single primary action per screen
    readonly property color actionDeep: "#C9763A"
    readonly property color alert:      "#FF5E7E"
    readonly property color ok:         "#4DFFB4"

    // ── type ────────────────────────────────────────────────────
    // Inter ships in assets/fonts (OFL) and is registered at startup, so the
    // same metrics hold on macOS, Linux/Yocto and QNX. Fallbacks avoid Qt's
    // costly alias scan if the bundle is stripped.
    readonly property var _families: Qt.fontFamilies()
    function _pick(list) { for (const f of list) if (_families.indexOf(f) >= 0) return f; return "" }
    readonly property string family: _pick(["Inter", "SF Pro Text", "Helvetica Neue", "Noto Sans", "DejaVu Sans"])
    readonly property string familyDisplay: _pick(["Inter Display", "Inter", "SF Pro Display", "Helvetica Neue", "Noto Sans"]) || family
    readonly property int fsCaption: 20
    readonly property int fsBody:    24
    readonly property int fsLead:    27
    readonly property int fsStatus:  27
    readonly property int fsTemp:    38

    // ── geometry (8-pt grid) ────────────────────────────────────
    readonly property int u: 8
    readonly property int radiusCard:   28
    readonly property int radiusInset:  18
    readonly property int radiusPill:   32
    readonly property int statusH: 78
    readonly property int dockH:   104

    // ── motion ──────────────────────────────────────────────────
    readonly property int durFast: 140
    readonly property int durMed:  260
    readonly property int durSlow: 520
    readonly property int easeStd: Easing.OutCubic

    function asset(name) { return Qt.resolvedUrl("../assets/img/" + name) }

    function icon(name, color, w) {
        const c = (color === undefined ? iconInk : color).toString().replace("#", "")
        return "image://icons/" + name + "?c=" + c + (w ? "&w=" + w : "")
    }
}
