// SPDX-License-Identifier: GPL-3.0-or-later

import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Controls.Material 2.15
import QtQuick.Layouts 1.15

ApplicationWindow {
    id: root
    visible: true
    width: 1120
    height: 760
    minimumWidth: 900
    minimumHeight: 620
    title: qsTr("q-sunshine desktop receiver")

    // Leading-edge layout stays visible while resizing the native window from
    // its left edge; fixed centered columns can otherwise slide under it.
    readonly property int pageMargin: 22
    readonly property bool compactPageLayout: width < 1040

    function syncPresentationMode() {
        presentationMode.currentIndex = presentationMode.indexOfValue(moonlight.profileDisplayMode)
        if (presentationMode.currentIndex < 0)
            presentationMode.currentIndex = 0
    }

    function reconnectWithPresentation() {
        if (!systemAuth.authenticated || profileNegotiation.busy || moonlight.pairing)
            return
        moonlight.startStream(moonlight.profileHost, moonlight.profileAppName,
                              moonlight.profileResolution, presentationMode.currentText)
    }

    Component.onCompleted: {
        syncPresentationMode()
        qsfClient.selectProfile(moonlight.currentProfileId)
        systemAuth.selectProfile(moonlight.currentProfileId)
    }

    Connections {
        target: moonlight
        function onProfileChanged() {
            root.syncPresentationMode()
            qsfClient.selectProfile(moonlight.currentProfileId)
            systemAuth.selectProfile(moonlight.currentProfileId)
        }
    }

    Connections {
        target: qsfClient
        function onConnectionProfileReceived(width, height, fps, bitrateKbps, videoCodec, qemuApplied) {
            guestResolution.editText = String(width) + "x" + String(height)
        }
    }

    header: ToolBar {
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 14
            anchors.rightMargin: 14
            spacing: 10

            Label {
                text: "q-sunshine"
                font.bold: true
                font.pixelSize: 18
            }
            Label {
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                elide: Text.ElideRight
                opacity: 0.75
                text: moonlight.running ? qsTr("VM desktop is streaming") :
                      (moonlight.streamBusy ? qsTr("Starting VM desktop") :
                       qsTr("Proxmox launch receiver"))
            }
            Label {
                Layout.maximumWidth: 230
                Layout.minimumWidth: 0
                elide: Text.ElideRight
                color: systemAuth.authenticated ? Material.accent : "#ffb74d"
                text: systemAuth.authenticated
                      ? qsTr("Authorized: %1").arg(systemAuth.subject)
                      : (systemAuth.authenticating ? qsTr("Redeeming launch") : qsTr("Awaiting launch"))
            }
        }
    }

    TabBar {
        id: tabs
        objectName: "mainTabs"
        width: parent.width
        TabButton { text: qsTr("Session") }
        TabButton { text: qsTr("Clipboard, files & display") }
        TabButton { text: qsTr("Diagnostics") }
    }

    StackLayout {
        anchors.top: tabs.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        currentIndex: tabs.currentIndex

        Item {
            ScrollView {
                id: connectionScroll
                objectName: "connectionScroll"
                anchors.fill: parent
                clip: true
                contentWidth: width
                contentHeight: connectionContent.implicitHeight + root.pageMargin * 2
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

                ColumnLayout {
                    id: connectionContent
                    objectName: "connectionContent"
                    width: Math.max(0, connectionScroll.availableWidth - root.pageMargin * 2)
                    x: root.pageMargin
                    y: root.pageMargin
                    spacing: 16

                    GroupBox {
                        title: qsTr("Proxmox VM launch")
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0

                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 10

                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                text: qsTr("This receiver is launched from the Proxmox VM Console menu. Proxmox has already checked your account and VM permissions; no password, PIN, host route, or TLS setting is entered here.")
                            }
                            Label {
                                id: launchReceiverStatus
                                objectName: "launchReceiverStatus"
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                font.bold: true
                                color: systemAuth.lastError.length > 0 ? "#ef9a9a" :
                                       (systemAuth.authenticated ? Material.accent : "#ffb74d")
                                text: systemAuth.status.length > 0 ? systemAuth.status :
                                      qsTr("Waiting for a one-use Proxmox launch file")
                            }
                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                opacity: 0.72
                                text: systemAuth.authenticated
                                      ? qsTr("The VM-specific authorization expires at %1 UTC. Transport routes and trust material are kept only in memory for this session.").arg(systemAuth.expiresAtUtc.toString("HH:mm:ss"))
                                      : qsTr("If redemption fails or the authorization has expired, return to Proxmox and choose Console again to download a fresh launch file.")
                            }
                            Label {
                                Layout.fillWidth: true
                                visible: systemAuth.lastError.length > 0
                                color: "#ef9a9a"
                                wrapMode: Text.Wrap
                                text: systemAuth.lastError
                            }
                        }
                    }

                    GroupBox {
                        title: qsTr("Presentation")
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0

                        GridLayout {
                            anchors.fill: parent
                            columns: root.compactPageLayout ? 1 : 2
                            columnSpacing: 12
                            rowSpacing: 10

                            Label { text: qsTr("Graphics window") }
                            RowLayout {
                                Layout.fillWidth: true
                                ComboBox {
                                    id: presentationMode
                                    objectName: "presentationMode"
                                    Layout.preferredWidth: 180
                                    model: ["windowed", "fullscreen"]
                                    enabled: systemAuth.authenticated && !profileNegotiation.busy &&
                                             !moonlight.pairing
                                }
                                Button {
                                    text: moonlight.running ? qsTr("Reconnect") : qsTr("Start desktop")
                                    highlighted: true
                                    enabled: systemAuth.authenticated && moonlight.canStartStream &&
                                             !profileNegotiation.busy && !moonlight.pairing &&
                                             (moonlight.running || !moonlight.streamBusy)
                                    onClicked: root.reconnectWithPresentation()
                                }
                                Item { Layout.fillWidth: true }
                            }
                            Label {
                                Layout.columnSpan: root.compactPageLayout ? 1 : 2
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                opacity: 0.72
                                text: qsTr("Only windowed and fullscreen modes are available. Reconnecting changes the graphics presentation; VM scanout size is negotiated separately below.")
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        Button {
                            text: qsTr("Disconnect")
                            enabled: systemAuth.authenticated || systemAuth.authenticating || moonlight.streamBusy
                            onClicked: systemAuth.logout()
                        }
                        Button {
                            text: qsTr("Cancel display handoff")
                            visible: profileNegotiation.busy
                            enabled: profileNegotiation.busy
                            onClicked: profileNegotiation.cancel()
                        }
                        Item { Layout.fillWidth: true }
                        Label {
                            Layout.maximumWidth: 480
                            wrapMode: Text.Wrap
                            opacity: 0.72
                            text: profileNegotiation.busy
                                  ? qsTr("The guest scanout handoff owns this session until its in-flight operation reaches a safe terminal state.")
                                  : qsTr("The VM media route is supplied by Proxmox for this launch only; it cannot be edited or saved in the client.")
                        }
                    }

                    Label {
                        Layout.fillWidth: true
                        visible: moonlight.lastError.length > 0
                        color: "#ef9a9a"
                        wrapMode: Text.Wrap
                        text: moonlight.lastError
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        text: moonlight.status
                    }
                }
            }
        }

        Item {
            ScrollView {
                id: companionScroll
                objectName: "companionScroll"
                anchors.fill: parent
                clip: true
                contentWidth: width
                contentHeight: companionContent.implicitHeight + root.pageMargin * 2
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

                ColumnLayout {
                    id: companionContent
                    objectName: "companionContent"
                    width: Math.max(0, companionScroll.availableWidth - root.pageMargin * 2)
                    x: root.pageMargin
                    y: root.pageMargin
                    spacing: 16

                    GroupBox {
                        title: qsTr("Companion session")
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0

                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 10
                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                text: qsTr("Activate clipboard, guest resize, and file transfer only after the graphics window visibly shows the selected VM desktop. The QSF endpoint and its TLS trust were supplied by the one-use Proxmox launch and are not editable here.")
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                Button {
                                    text: qsfClient.sessionActive
                                          ? qsTr("Deactivate companion")
                                          : qsTr("Activate for visible desktop")
                                    enabled: systemAuth.authenticated && !profileNegotiation.busy &&
                                             ((moonlight.running && !moonlight.streamStopping) ||
                                              qsfClient.sessionActive)
                                    onClicked: qsfClient.sessionActive = !qsfClient.sessionActive
                                }
                                Label {
                                    Layout.fillWidth: true
                                    wrapMode: Text.Wrap
                                    opacity: 0.72
                                    text: qsfClient.sessionActive
                                          ? qsTr("The companion is verifying the VM-specific session.")
                                          : qsTr("Inactive sessions cannot access clipboard, resize, or files.")
                                }
                            }
                        }
                    }

                    GroupBox {
                        title: qsTr("Clipboard and display")
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0

                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 10

                            RowLayout {
                                Layout.fillWidth: true
                                Label { text: qsTr("Preferred VM desktop size") }
                                ComboBox {
                                    id: guestResolution
                                    objectName: "guestResolution"
                                    Layout.preferredWidth: 155
                                    enabled: !profileNegotiation.busy
                                    editable: true
                                    model: ["1280x720", "1600x900", "1920x1080", "2560x1440", "3840x2160"]
                                    currentIndex: 2
                                }
                                Button {
                                    text: qsTr("Request resize")
                                    enabled: !profileNegotiation.busy && qsfClient.ready
                                    onClicked: qsfClient.requestResizeText(guestResolution.editText)
                                }
                                Item { Layout.fillWidth: true }
                            }
                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                opacity: 0.72
                                text: qsTr("This is the explicit VM desktop size used for capability negotiation. A direct resize asks the guest to apply it; choosing an optimal profile below also selects the matching decoder and encoder settings.")
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                Button {
                                    text: qsTr("Choose optimal stream profile")
                                    highlighted: true
                                    enabled: qsfClient.ready && moonlight.running &&
                                             !moonlight.streamStopping && !profileNegotiation.busy
                                    onClicked: profileNegotiation.negotiate(
                                                           guestResolution.editText,
                                                           moonlight.videoDecoder)
                                }
                                Label {
                                    Layout.fillWidth: true
                                    wrapMode: Text.Wrap
                                    opacity: 0.72
                                    text: qsTr("The selected client size is negotiated across the decoder, Sunshine encoder, QEMU, and VirGL guest. The stream restarts only after the guest scanout handoff is confirmed.")
                                }
                            }
                            Label {
                                Layout.fillWidth: true
                                visible: moonlight.profileFps > 0
                                opacity: 0.72
                                text: qsTr("Current stream profile: %1 · %2 FPS · %3 Kbps · %4")
                                      .arg(moonlight.profileResolution)
                                      .arg(moonlight.profileFps)
                                      .arg(moonlight.profileBitrateKbps)
                                      .arg(moonlight.profileVideoCodec)
                            }

                            Switch {
                                text: qsTr("Synchronize plain-text clipboard while this stream is active")
                                enabled: !profileNegotiation.busy && qsfClient.ready
                                checked: qsfClient.clipboardSyncEnabled
                                onToggled: qsfClient.clipboardSyncEnabled = checked
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                Label { text: qsTr("Initial clipboard on activation") }
                                ComboBox {
                                    id: initialClipboardDirection
                                    Layout.preferredWidth: 175
                                    enabled: !profileNegotiation.busy && !qsfClient.sessionActive
                                    textRole: "text"
                                    valueRole: "value"
                                    model: [
                                        { "text": qsTr("Client → guest"), "value": "client" },
                                        { "text": qsTr("Guest → client"), "value": "guest" }
                                    ]
                                    Component.onCompleted: {
                                        currentIndex = indexOfValue(qsfClient.initialClipboardDirection)
                                        if (currentIndex < 0)
                                            currentIndex = 0
                                    }
                                    onActivated: qsfClient.initialClipboardDirection = currentValue
                                }
                                Item { Layout.fillWidth: true }
                            }
                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                opacity: 0.72
                                text: qsTr("Choose an initial direction because existing clipboards cannot be merged. Later edits synchronize in both directions. Text is non-NUL UTF-8 and limited to 1 MiB.")
                            }

                        }
                    }

                    GroupBox {
                        title: qsTr("Constrained file transfer")
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0

                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 10
                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                text: qsTr("Files use the VM companion channel, not Moonlight packets: 2 MiB maximum and safe ASCII basenames. Uploads go to the guest inbox; downloads read its outbox.")
                            }
                            GridLayout {
                                Layout.fillWidth: true
                                columns: root.compactPageLayout ? 1 : 3
                                columnSpacing: 10
                                rowSpacing: 8

                                Label { text: qsTr("Local upload path") }
                                TextField {
                                    id: uploadPath
                                    Layout.fillWidth: true
                                    enabled: !profileNegotiation.busy
                                    placeholderText: "/local/path/file.txt"
                                }
                                Button {
                                    text: qsTr("Upload")
                                    enabled: !profileNegotiation.busy && qsfClient.ready
                                    onClicked: qsfClient.uploadFile(uploadPath.text, uploadName.text)
                                }
                                Label { text: qsTr("Guest upload name") }
                                TextField {
                                    id: uploadName
                                    Layout.fillWidth: true
                                    enabled: !profileNegotiation.busy
                                    placeholderText: qsTr("Optional basename")
                                }
                                Item { }
                                Label { text: qsTr("Guest outbox name") }
                                TextField {
                                    id: downloadName
                                    Layout.fillWidth: true
                                    enabled: !profileNegotiation.busy
                                    placeholderText: "guest-download.txt"
                                }
                                Button {
                                    text: qsTr("Download")
                                    enabled: !profileNegotiation.busy && qsfClient.ready
                                    onClicked: qsfClient.downloadFile(downloadName.text, downloadPath.text)
                                }
                                Label { text: qsTr("Local download path") }
                                TextField {
                                    id: downloadPath
                                    Layout.fillWidth: true
                                    enabled: !profileNegotiation.busy
                                    placeholderText: "/local/path/download.txt"
                                }
                                Item { }
                            }
                        }
                    }

                    Label {
                        Layout.fillWidth: true
                        visible: profileNegotiation.lastError.length > 0
                        color: "#ef9a9a"
                        wrapMode: Text.Wrap
                        text: profileNegotiation.lastError
                    }
                    Label {
                        Layout.fillWidth: true
                        visible: profileNegotiation.busy
                        wrapMode: Text.Wrap
                        text: profileNegotiation.status
                    }
                    Label {
                        Layout.fillWidth: true
                        visible: qsfClient.lastError.length > 0
                        color: "#ef9a9a"
                        wrapMode: Text.Wrap
                        text: qsfClient.lastError
                    }
                    Label {
                        Layout.fillWidth: true
                        visible: qsfClient.lastResult.length > 0
                        color: "#a5d6a7"
                        wrapMode: Text.Wrap
                        text: qsfClient.lastResult
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        text: qsfClient.status
                    }
                }
            }
        }

        Item {
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: root.pageMargin
                spacing: 12
                Label {
                    text: qsTr("Moonlight process output")
                    font.bold: true
                }
                TextArea {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    readOnly: true
                    wrapMode: TextEdit.Wrap
                    text: moonlight.recentOutput
                    placeholderText: qsTr("No Moonlight process output yet.")
                }
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    opacity: 0.72
                    text: qsTr("Security boundary: Proxmox authorizes the VM launch, then the broker returns short-lived VM-bound media and companion routes. The launch claim, session ticket, CA material, and routes are not saved in the client profile.")
                }
            }
        }
    }
}
