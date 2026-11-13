import QtQuick
import QtQuick.Window
import "components"

Window {
    id: win
    width: 1920; height: 1080
    visible: true
    color: Theme.bgTop
    title: "Cabin Assistant"

    // Author once at 1920x1080; letterbox-free uniform scale to the panel.
    Item {
        id: stage
        width: 1920; height: 1080
        anchors.centerIn: parent
        scale: Math.min(win.width / width, win.height / height)

        // Backend publishes `assistant.level` at ~30 Hz; interpolate at vsync
        // (60/120 Hz) so meters and glows never step. Fast attack, slow release.
        property real level: 0
        property real energy: 0
        FrameAnimation {
            running: true
            onTriggered: {
                const target = assistant.level
                const k = 1 - Math.exp(-frameTime * (target > stage.level ? 28 : 9))
                stage.level += (target - stage.level) * k
                const e = assistant.state === "speaking" ? 0.55 + stage.level * 0.45
                        : assistant.state === "listening" ? 0.35 + stage.level * 0.3 : 0.2
                stage.energy += (e - stage.energy) * (1 - Math.exp(-frameTime * 4))
            }
        }

        // ── backdrop ───────────────────────────────────────────
        Rectangle {
            anchors.fill: parent
            gradient: Gradient {
                GradientStop { position: 0.00; color: Theme.bgTop }
                GradientStop { position: 0.45; color: Theme.bgMid }
                GradientStop { position: 1.00; color: Theme.bgBottom }
            }
        }
        Image { source: Theme.asset("amb_blue.png"); width: 1100; height: 1100; x: 1100; y: -520; opacity: 0.22 }
        Image { source: Theme.asset("amb_violet.png"); width: 900; height: 700; x: 560; y: 620; opacity: 0.35 }

        AvatarStage {
            id: avatar; x: 0; y: Theme.statusH; width: 960; height: 820
            rig: avatarRig
            mode: assistant.state
            level: stage.level
            energy: stage.energy
            visemeJaw: assistant.visemeJaw
            visemeRound: assistant.visemeRound
            visemesLive: assistant.visemesLive
        }

        LightRibbon {
            x: 0; y: 740; width: 1000; height: 260
            energy: stage.energy
        }

        StatusBar {
            id: status; width: parent.width; z: 50
            driverName: typeof driverName !== "undefined" ? driverName : "Driver"
            outsideTemp: assistant.outside
            simulation: assistant.simulation
            tour: assistant.tour
            notice: assistant.notice
            noticeCode: assistant.noticeCode
        }

        Loader { id: driverSheet; active: assistant.driverOpen; source: "components/DriverSheet.qml"; anchors.fill: parent; anchors.topMargin: Theme.statusH + 12; anchors.leftMargin: 24; anchors.rightMargin: 24; anchors.bottomMargin: 24; z: 40 }
        Binding { target: driverSheet.item; property: "driver"; value: assistant.driver; when: driverSheet.status === Loader.Ready }

        // ── right column: conversation or navigation ───────────
        readonly property bool navigating: assistant.mapOpen || (assistant.nav && assistant.nav.map !== undefined)

        Item {
            id: convo
            x: 822; y: Theme.statusH + 26; width: 1060
            visible: opacity > 0
            opacity: stage.navigating ? 0 : 1
            Behavior on opacity { NumberAnimation { duration: Theme.durMed } }
            readonly property bool hasAsk: assistant.userText !== "" || (assistant.state === "listening")
            readonly property bool hasReply: assistant.replyText !== "" || assistant.cardVisible
            readonly property bool empty: !hasAsk && !hasReply && assistant.state === "idle"

            Item {
                id: askRow
                width: parent.width; height: Math.max(chip.height + 6, ask.height)
                opacity: convo.hasAsk ? 1 : 0
                transform: Translate { y: convo.hasAsk ? 0 : 14
                    Behavior on y { NumberAnimation { duration: Theme.durMed; easing.type: Easing.OutCubic } } }
                Behavior on opacity { NumberAnimation { duration: Theme.durMed } }

                Rectangle {     // driver chip
                    id: chip
                    anchors.right: parent.right; y: 6
                    width: 72; height: 72; radius: 36
                    color: "#232B4C"; border.width: 1; border.color: Theme.hairlineHi
                    Icon { anchors.centerIn: parent; name: "user"; size: 40; color: "#E6EBFF" }
                }
                UserBubble {
                    id: ask
                    anchors.right: chip.left; anchors.rightMargin: 14
                    maxTextWidth: 640
                    text: assistant.userText !== "" ? assistant.userText : "…"
                    partial: assistant.userPartial || assistant.state === "listening"
                }
            }

            AssistantCard {
                id: card
                anchors.top: askRow.bottom; anchors.topMargin: 28
                width: parent.width
                headline: assistant.replyText
                streaming: assistant.state === "speaking"
                card: assistant.card
                opacity: convo.hasReply ? 1 : 0
                transform: Translate { y: convo.hasReply ? 0 : 22
                    Behavior on y { NumberAnimation { duration: Theme.durSlow; easing.type: Easing.OutCubic } } }
                Behavior on opacity { NumberAnimation { duration: Theme.durMed } }
                onActionTriggered: (key) => assistant.triggerAction(key)
                Connections { target: assistant; function onActionPressed(k) { card.flash(k) } }
            }

            // first-run / idle suggestions
            Column {
                y: 40; width: parent.width; spacing: 26
                opacity: convo.empty ? 1 : 0
                visible: opacity > 0
                Behavior on opacity { NumberAnimation { duration: Theme.durSlow } }
                Text {
                    width: parent.width; horizontalAlignment: Text.AlignRight
                    text: "Good " + (new Date().getHours() < 12 ? "morning" : new Date().getHours() < 18 ? "afternoon" : "evening")
                          + (status.driverName && status.driverName !== "Driver" ? ", " + status.driverName : "")
                    color: Theme.text; font.family: Theme.familyDisplay; font.pixelSize: 44; font.weight: Font.Light
                }
                Text {
                    width: parent.width; horizontalAlignment: Text.AlignRight
                    text: assistant.providers.voice ? (assistant.providers.handsfree ? "Just start talking — or try one of these"
                                                                                      : "Tap the orb to talk — or try one of these")
                                                    : "Type a request — or try one of these"
                    color: Theme.textDim; font.family: Theme.family; font.pixelSize: 24
                }
                Flow {
                    width: parent.width; spacing: 14; layoutDirection: Qt.RightToLeft
                    Repeater {
                        model: ["Plan a weekend trip to Lake Tahoe", "Find a coffee shop nearby",
                                "What's the weather tomorrow?", "Set the cabin to 70 and warm my seat",
                                "What's the latest tech news?"]
                        PillButton { text: modelData; icon: "sparkle"; onClicked: assistant.submitText(modelData) }
                    }
                }
            }
        }

        NavPanel {
            x: 822; y: Theme.statusH + 26; width: 1060
            opacity: stage.navigating ? 1 : 0
            visible: opacity > 0
            Behavior on opacity { NumberAnimation { duration: Theme.durMed } }
            nav: assistant.nav
            onClosed: assistant.closeNav()
        }

        // status caption (Listening / Thinking …) above the orb
        Text {
            id: caption
            anchors.horizontalCenter: parent.horizontalCenter
            y: voice.y - 64
            text: assistant.caption + (assistant.state === "thinking" ? dots.s : "")
            opacity: assistant.caption !== "" && !assistant.keyboardOpen ? 0.9 : 0
            Behavior on opacity { NumberAnimation { duration: Theme.durMed } }
            color: Theme.textDim
            font.family: Theme.family; font.pixelSize: Theme.fsCaption; font.letterSpacing: 1.2
            font.capitalization: Font.AllUppercase
            QtObject { id: dots; property string s: ""; property int n: 0 }
            Timer { interval: 380; repeat: true; running: assistant.state === "thinking"
                    onTriggered: { dots.n = (dots.n + 1) % 4; dots.s = ".".repeat(dots.n) } }
        }

        // ── keyboard entry ─────────────────────────────────────
        Rectangle {
            id: entry
            width: 900; height: 76; radius: 38
            anchors.horizontalCenter: parent.horizontalCenter
            y: voice.y - 104
            opacity: assistant.keyboardOpen ? 1 : 0
            visible: opacity > 0
            Behavior on opacity { NumberAnimation { duration: Theme.durMed } }
            color: Qt.rgba(0.07, 0.09, 0.22, 0.95)
            border.width: 1.5; border.color: field.activeFocus ? Qt.rgba(0.6, 0.66, 1, 0.7) : Theme.hairlineHi
            onVisibleChanged: if (visible) field.forceActiveFocus()
            TextInput {
                id: field
                x: 34; width: parent.width - 140; anchors.verticalCenter: parent.verticalCenter
                color: Theme.text; font.family: Theme.family; font.pixelSize: 26
                selectionColor: Theme.accent; clip: true
                Text { anchors.fill: parent; verticalAlignment: Text.AlignVCenter
                       visible: field.text === ""; text: "Ask anything…"; color: Theme.textFaint
                       font: field.font }
                Keys.onReturnPressed: send()
                Keys.onEnterPressed: send()
                Keys.onEscapePressed: { assistant.keyboardOpen = false; keyCatcher.forceActiveFocus() }
                function send() { assistant.submitText(text); text = ""; keyCatcher.forceActiveFocus() }
            }
            Rectangle {
                anchors.right: parent.right; anchors.rightMargin: 10; anchors.verticalCenter: parent.verticalCenter
                width: 96; height: 56; radius: 28; color: field.text ? Theme.action : Qt.rgba(1, 1, 1, 0.08)
                Behavior on color { ColorAnimation { duration: Theme.durFast } }
                Icon { anchors.centerIn: parent; name: "send"; size: 28; color: field.text ? "#1A1206" : Theme.textDim }
                MouseArea { anchors.fill: parent; onClicked: field.send() }
            }
        }

        // ── voice controls ─────────────────────────────────────
        Item {
            id: voice
            width: parent.width; height: 160
            y: 1080 - Theme.dockH - height + 6

            AudioBars { anchors.right: orb.left; anchors.rightMargin: 34; anchors.verticalCenter: orb.verticalCenter
                        count: 13; level: stage.level; mirrored: true; opacity: 0.85 }
            VoiceOrb { id: orb; anchors.horizontalCenter: parent.horizontalCenter; anchors.verticalCenter: parent.verticalCenter
                       mode: assistant.state; level: stage.level
                       onPressed: assistant.toggleListening() }
            AudioBars { anchors.left: orb.right; anchors.leftMargin: 34; anchors.verticalCenter: orb.verticalCenter
                        count: 13; level: stage.level; maxH: 60 }

            Rectangle {     // keyboard
                x: 1300; anchors.verticalCenter: orb.verticalCenter
                width: 112; height: 66; radius: 33
                color: assistant.keyboardOpen ? Qt.rgba(1.0, 0.70, 0.40, 0.18) : Qt.rgba(0.55, 0.63, 1.0, 0.07)
                border.width: 1
                border.color: assistant.keyboardOpen ? Qt.rgba(1.0, 0.74, 0.45, 0.8) : Qt.rgba(0.75, 0.82, 1.0, 0.30)
                Icon { anchors.centerIn: parent; name: "keyboard"; size: 38 }
                MouseArea { anchors.fill: parent; anchors.margins: -10
                            onClicked: assistant.keyboardOpen = !assistant.keyboardOpen }
            }
        }

        Dock {
            id: dock; width: parent.width; y: 1080 - height
            cabinTempF: Math.round(assistant.vehicle.cabin_temp_f || 72)
            seatHeat: assistant.vehicle.seat_heat || 0
            onTempNudged: (d) => assistant.nudgeCabinTemp(d)
            onSeatHeatClicked: assistant.cycleSeatHeat()
            onAppSelected: (i, key) => {
                if (key === "nav") assistant.openLocationMap()
                else if (key === "home") assistant.closeLocationMap()
                else toast.show("Opens the " + key + " app on the vehicle")
            }
        }

        Toast {
            id: toast
            anchors.horizontalCenter: parent.horizontalCenter
            y: Theme.statusH + 14; z: 50
            Connections { target: assistant; function onToast(m) { toast.show(m) } }
        }

        // perf HUD (D): fps measured on the render thread's frame clock
        Rectangle {
            id: perf
            visible: typeof showPerf !== "undefined" && showPerf
            x: 24; y: Theme.statusH + 12; width: 440; height: 112; radius: 12
            color: Qt.rgba(0, 0, 0, 0.55); border.width: 1; border.color: Theme.hairline
            property int frames: 0
            property real acc: 0
            property real fps: 0
            property real worst: 0
            property real _w: 0
            FrameAnimation {
                running: perf.visible
                onTriggered: {
                    perf.frames++; perf.acc += frameTime; perf._w = Math.max(perf._w, frameTime)
                    if (perf.acc >= 0.5) { perf.fps = perf.frames / perf.acc; perf.worst = perf._w * 1000
                                           perf.frames = 0; perf.acc = 0; perf._w = 0
                                           if (perfLog) console.info("perf " + perf.fps.toFixed(1) + " fps worst "
                                                                      + perf.worst.toFixed(1) + " ms state " + assistant.state) }
                }
            }
            Text {
                anchors.left: parent.left; anchors.right: parent.right
                anchors.leftMargin: 12; anchors.rightMargin: 12
                anchors.verticalCenter: parent.verticalCenter
                elide: Text.ElideRight
                color: perf.fps >= 55 ? Theme.ok : perf.fps >= 30 ? Theme.action : Theme.alert
                font.family: "Menlo"; font.pixelSize: 16
                readonly property var apis: ["?", "Software", "OpenVG", "OpenGL", "D3D11", "Vulkan", "Metal", "Null", "D3D12"]
                text: perf.fps.toFixed(0) + " fps · worst " + perf.worst.toFixed(1) + " ms\n"
                      + (apis[GraphicsInfo.api] || "?") + " · scale " + stage.scale.toFixed(2) + "\n"
                      + (assistant.providers.llm || "demo") + "\n"
                      + "STT " + (assistant.providers.stt || "—") + " · TTS " + (assistant.providers.tts || "—")
            }
        }

        DiagnosticsPanel { visible: perf.visible; x: perf.x; y: perf.y + perf.height + 12; z: 40 }

        Item {      // shortcuts (simulator keys are no-ops in live mode)
            id: keyCatcher
            focus: true
            Keys.onPressed: (e) => {
                const sim = (typeof simulator !== "undefined") ? simulator : null
                switch (e.key) {
                case Qt.Key_Space: assistant.toggleListening(); break
                case Qt.Key_N: if (sim) sim.next(); break
                case Qt.Key_P: if (sim) sim.togglePause(); break
                case Qt.Key_D: perf.visible = !perf.visible; break
                case Qt.Key_K: assistant.keyboardOpen = !assistant.keyboardOpen; break
                case Qt.Key_F: win.visibility = win.visibility === Window.FullScreen ? Window.Windowed : Window.FullScreen; break
                case Qt.Key_Escape: if (win.visibility === Window.FullScreen) win.visibility = Window.Windowed; break
                default: return
                }
                e.accepted = true
            }
        }
    }
}
