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
    title: qsTr("q-sunshine desktop client")

    function loadDesktopFields() {
        profileNameField.text = moonlight.currentProfileId
        hostField.text = moonlight.profileHost
        appField.text = moonlight.profileAppName
        initialResolution.editText = moonlight.profileResolution
        displayMode.currentIndex = displayMode.indexOfValue(moonlight.profileDisplayMode)
        if (displayMode.currentIndex < 0)
            displayMode.currentIndex = 0
        videoDecoder.currentIndex = videoDecoder.indexOfValue(moonlight.videoDecoder)
        if (videoDecoder.currentIndex < 0)
            videoDecoder.currentIndex = 0
        moonlightPathField.text = moonlight.binaryPath
    }

    function saveDesktopProfile() {
        var saved = moonlight.saveProfile(profileNameField.text, hostField.text,
                                          appField.text, initialResolution.editText,
                                          displayMode.currentText)
        if (saved) {
            qsfClient.selectProfile(moonlight.currentProfileId)
            root.loadDesktopFields()
            root.loadQsfFields()
        }
        return saved
    }

    function loadQsfFields() {
        qsfHostField.text = qsfClient.host
        qsfPortField.text = qsfClient.port > 0 ? String(qsfClient.port) : "48122"
        qsfServerNameField.text = qsfClient.serverName
        qsfCaField.text = qsfClient.caFile
        qsfCertificateField.text = qsfClient.clientCertificateFile
        qsfKeyField.text = qsfClient.clientKeyFile
        initialClipboardDirection.currentIndex =
                initialClipboardDirection.indexOfValue(qsfClient.initialClipboardDirection)
        if (initialClipboardDirection.currentIndex < 0)
            initialClipboardDirection.currentIndex = 0
    }

    function saveQsfFields() {
        return qsfClient.applyConfigurationText(qsfHostField.text,
                                                qsfPortField.text,
                                                qsfServerNameField.text,
                                                qsfCaField.text,
                                                qsfCertificateField.text,
                                                qsfKeyField.text)
    }

    Component.onCompleted: {
        loadDesktopFields()
        qsfClient.selectProfile(moonlight.currentProfileId)
        loadQsfFields()
    }

    Connections {
        target: moonlight
        function onProfileChanged() {
            root.loadDesktopFields()
            qsfClient.selectProfile(moonlight.currentProfileId)
            root.loadQsfFields()
        }
        function onProfilesChanged() { root.loadDesktopFields() }
    }

    Connections {
        target: qsfClient
        function onProfileChanged() { root.loadQsfFields() }
        function onConfigurationChanged() { root.loadQsfFields() }
    }

    header: ToolBar {
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 14
            anchors.rightMargin: 14

            Label {
                text: "q-sunshine"
                font.bold: true
                font.pixelSize: 18
            }
            Label {
                text: moonlight.running ? qsTr("Moonlight graphics process running — verify that video is visible") :
                      (moonlight.streamBusy ? qsTr("Starting Moonlight graphics stream") : qsTr("Desktop session manager"))
                opacity: 0.75
                Layout.fillWidth: true
            }
            Label {
                text: qsfClient.ready ? qsTr("QSF ready") :
                      (qsfClient.sessionActive ? qsTr("QSF verifying") : qsTr("QSF inactive"))
                color: qsfClient.ready ? Material.accent : "#ffb74d"
            }
        }
    }

    TabBar {
        id: tabs
        width: parent.width
        TabButton { text: qsTr("Connection") }
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
                anchors.fill: parent
                clip: true

                ColumnLayout {
                    width: Math.max(760, root.width - 48)
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.margins: 22
                    spacing: 16

                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        text: qsTr("Moonlight handles only the GameStream video, audio, and input window. This Qt shell owns profiles and the authenticated q-sunshine companion channel.")
                    }

                    GroupBox {
                        title: qsTr("Desktop profile")
                        Layout.fillWidth: true

                        GridLayout {
                            columns: 2
                            anchors.fill: parent
                            columnSpacing: 12
                            rowSpacing: 10

                            Label { text: qsTr("Saved profile") }
                            RowLayout {
                                Layout.fillWidth: true
                                ComboBox {
                                    id: profileSelector
                                    Layout.preferredWidth: 210
                                    enabled: !moonlight.streamBusy && !moonlight.pairing
                                    model: moonlight.profileIds
                                    currentIndex: {
                                        var index = moonlight.profileIds.indexOf(moonlight.currentProfileId)
                                        return index < 0 ? 0 : index
                                    }
                                    onActivated: {
                                        if (moonlight.selectProfile(currentText)) {
                                            root.loadDesktopFields()
                                            qsfClient.selectProfile(moonlight.currentProfileId)
                                            root.loadQsfFields()
                                        } else {
                                            root.loadDesktopFields()
                                        }
                                    }
                                }
                                TextField {
                                    id: profileNameField
                                    Layout.fillWidth: true
                                    enabled: !moonlight.streamBusy && !moonlight.pairing
                                    placeholderText: qsTr("Profile name; saving a new name creates a profile")
                                    maximumLength: 64
                                }
                                Button {
                                    text: qsTr("Save profile")
                                    enabled: !moonlight.streamBusy && !moonlight.pairing
                                    onClicked: root.saveDesktopProfile()
                                }
                            }

                            Label { text: qsTr("Sunshine host") }
                            TextField {
                                id: hostField
                                Layout.fillWidth: true
                                enabled: !moonlight.streamBusy && !moonlight.pairing
                                placeholderText: "192.168.64.25"
                            }

                            Label { text: qsTr("Application") }
                            TextField {
                                id: appField
                                Layout.fillWidth: true
                                enabled: !moonlight.pairing && (moonlight.running || !moonlight.streamBusy)
                                // App, stream size, and presentation deliberately remain editable
                                // during a session: Connect performs a controlled reconnect.
                            }

                            Label { text: qsTr("Initial stream size") }
                            ComboBox {
                                id: initialResolution
                                Layout.fillWidth: true
                                enabled: !moonlight.pairing && (moonlight.running || !moonlight.streamBusy)
                                editable: true
                                model: ["1280x720", "1600x900", "1920x1080", "2560x1440", "3840x2160"]
                            }

                            Label { text: qsTr("Presentation") }
                            ComboBox {
                                id: displayMode
                                Layout.fillWidth: true
                                enabled: !moonlight.pairing && (moonlight.running || !moonlight.streamBusy)
                                model: ["windowed", "fullscreen", "borderless"]
                            }

                            Label { text: qsTr("Video decoder") }
                            ComboBox {
                                id: videoDecoder
                                Layout.fillWidth: true
                                enabled: !moonlight.streamBusy && !moonlight.pairing
                                model: ["auto", "software", "hardware"]
                                onActivated: {
                                    moonlight.videoDecoder = currentValue
                                    currentIndex = indexOfValue(moonlight.videoDecoder)
                                }
                            }

                            Label { text: qsTr("Moonlight executable") }
                            TextField {
                                id: moonlightPathField
                                Layout.fillWidth: true
                                enabled: !moonlight.streamBusy && !moonlight.pairing
                                text: moonlight.binaryPath
                                onEditingFinished: {
                                    moonlight.binaryPath = text
                                    // Restore the persisted value when an empty path was
                                    // rejected instead of leaving the editor misleading.
                                    text = moonlight.binaryPath
                                }
                            }
                        }
                    }

                    GroupBox {
                        title: qsTr("Pairing")
                        Layout.fillWidth: true

                        RowLayout {
                            anchors.fill: parent
                            Label { text: qsTr("Four-digit PIN to enter in Sunshine") }
                            TextField {
                                id: pinField
                                Layout.preferredWidth: 130
                                enabled: !moonlight.streamBusy && !moonlight.pairing
                                inputMethodHints: Qt.ImhDigitsOnly
                                maximumLength: 4
                                echoMode: TextInput.Password
                            }
                            Button {
                                text: qsTr("Pair")
                                enabled: !moonlight.streamBusy && !moonlight.pairing
                                onClicked: {
                                    if (!root.saveDesktopProfile())
                                        return
                                    moonlight.binaryPath = moonlightPathField.text
                                    moonlight.pair(moonlight.profileHost, pinField.text)
                                    pinField.clear()
                                }
                            }
                            Button {
                                text: qsTr("Cancel pairing")
                                visible: moonlight.pairing
                                enabled: moonlight.pairing
                                onClicked: moonlight.cancelPairing()
                            }
                            Item { Layout.fillWidth: true }
                            Label {
                                text: qsTr("This client supplies the PIN to Moonlight; enter the same PIN in Sunshine's pairing UI. Moonlight stores the host certificate in its normal local profile.")
                                opacity: 0.7
                                Layout.maximumWidth: 420
                                wrapMode: Text.Wrap
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true

                        Button {
                            text: moonlight.running ? qsTr("Reconnect with these settings") :
                                  (moonlight.streamBusy ? qsTr("Starting desktop") : qsTr("Connect desktop"))
                            highlighted: true
                            enabled: !moonlight.pairing && (moonlight.running || !moonlight.streamBusy)
                            onClicked: {
                                if (!root.saveDesktopProfile())
                                    return
                                moonlight.binaryPath = moonlightPathField.text
                                moonlight.startStream(moonlight.profileHost, moonlight.profileAppName,
                                                      moonlight.profileResolution,
                                                      moonlight.profileDisplayMode)
                            }
                        }
                        Button {
                            text: qsTr("Disconnect")
                            enabled: moonlight.streamBusy
                            onClicked: moonlight.stopStream()
                        }
                        Item { Layout.fillWidth: true }
                        Label {
                            text: qsTr("Changing the app, presentation, or initial stream size while connected performs a controlled graphics-stream reconnect. Guest resize is separate in the companion tab.")
                            Layout.maximumWidth: 510
                            wrapMode: Text.Wrap
                            opacity: 0.75
                        }
                    }

                    Label {
                        Layout.fillWidth: true
                        visible: moonlight.lastError.length > 0
                        text: moonlight.lastError
                        color: "#ef9a9a"
                        wrapMode: Text.Wrap
                    }
                    Label {
                        Layout.fillWidth: true
                        text: moonlight.status
                        wrapMode: Text.Wrap
                    }
                }
            }
        }

        Item {
            ScrollView {
                anchors.fill: parent
                clip: true

                ColumnLayout {
                    width: Math.max(760, root.width - 48)
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.margins: 22
                    spacing: 16

                    GroupBox {
                        title: qsTr("QSF TLS 1.3 companion for this desktop profile")
                        Layout.fillWidth: true

                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 10

                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                text: qsTr("The gateway receives an mTLS client certificate and reads the host-local QSF token itself. The token never crosses the network. Paths are stored per desktop profile; use a private key protected by filesystem permissions.")
                            }

                            GridLayout {
                                columns: 2
                                Layout.fillWidth: true
                                enabled: !qsfClient.sessionActive
                                columnSpacing: 12
                                rowSpacing: 8

                                Label { text: qsTr("Gateway host") }
                                TextField { id: qsfHostField; Layout.fillWidth: true; placeholderText: "qsf.example" }
                                Label { text: qsTr("TCP port") }
                                TextField { id: qsfPortField; Layout.fillWidth: true; inputMethodHints: Qt.ImhDigitsOnly }
                                Label { text: qsTr("TLS server name") }
                                TextField { id: qsfServerNameField; Layout.fillWidth: true; placeholderText: qsTr("Optional; defaults to gateway host") }
                                Label { text: qsTr("Gateway CA PEM") }
                                TextField { id: qsfCaField; Layout.fillWidth: true; placeholderText: "/path/to/qsf-server-ca.crt" }
                                Label { text: qsTr("Client certificate PEM") }
                                TextField { id: qsfCertificateField; Layout.fillWidth: true; placeholderText: "/path/to/qsf-client.crt" }
                                Label { text: qsTr("Client private key PEM") }
                                TextField { id: qsfKeyField; Layout.fillWidth: true; echoMode: TextInput.Password; placeholderText: "/path/to/qsf-client.key" }
                            }

                            RowLayout {
                                Button {
                                    text: qsTr("Save")
                                    enabled: !qsfClient.sessionActive
                                    onClicked: root.saveQsfFields()
                                }
                                Button {
                                    text: qsTr("Test gateway")
                                    enabled: !qsfClient.sessionActive
                                    onClicked: {
                                        if (root.saveQsfFields())
                                            qsfClient.testConnection()
                                    }
                                }
                                Item { Layout.fillWidth: true }
                                Label { text: qsfClient.profileId; opacity: 0.65 }
                            }
                        }
                    }

                    GroupBox {
                        title: qsTr("Companion session activation")
                        Layout.fillWidth: true

                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 8

                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                text: qsTr("Activate QSF only after the Moonlight video window visibly shows the selected desktop. A child process starting is not treated as proof of an authenticated GameStream session, and no log parsing is used as an authority signal.")
                            }
                            RowLayout {
                                Button {
                                    text: qsfClient.sessionActive ? qsTr("Deactivate QSF companion") : qsTr("Activate QSF for visible stream")
                                    enabled: (moonlight.running && !moonlight.streamStopping) || qsfClient.sessionActive
                                    onClicked: qsfClient.sessionActive = !qsfClient.sessionActive
                                }
                                Label {
                                    Layout.fillWidth: true
                                    wrapMode: Text.Wrap
                                    opacity: 0.75
                                    text: qsfClient.sessionActive
                                          ? qsTr("The profile is being verified with its mTLS gateway; ending or reconnecting Moonlight cancels every QSF operation.")
                                          : qsTr("Inactive profiles cannot send clipboard, resize, upload, or download operations.")
                                }
                            }
                        }
                    }

                    GroupBox {
                        title: qsTr("Clipboard and display")
                        Layout.fillWidth: true

                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 10

                            Switch {
                                text: qsTr("Synchronize plain-text clipboard while this stream is active")
                                enabled: qsfClient.ready
                                checked: qsfClient.clipboardSyncEnabled
                                onToggled: qsfClient.clipboardSyncEnabled = checked
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                Label { text: qsTr("Initial clipboard on activation") }
                                ComboBox {
                                    id: initialClipboardDirection
                                    Layout.preferredWidth: 170
                                    enabled: !qsfClient.sessionActive
                                    textRole: "text"
                                    valueRole: "value"
                                    model: [
                                        { "text": qsTr("Client → guest"), "value": "client" },
                                        { "text": qsTr("Guest → client"), "value": "guest" }
                                    ]
                                    onActivated: qsfClient.initialClipboardDirection = currentValue
                                }
                                Item { Layout.fillWidth: true }
                            }
                            Label {
                                Layout.fillWidth: true
                                text: qsTr("Choose an explicit initial direction: existing clipboards cannot be merged safely. Later changes synchronize both ways. Text is non-NUL UTF-8 and limited to 1 MiB; turning sync off cancels outstanding clipboard requests immediately.")
                                opacity: 0.75
                                wrapMode: Text.Wrap
                            }

                            RowLayout {
                                Label { text: qsTr("Guest resolution") }
                                ComboBox {
                                    id: guestResolution
                                    Layout.preferredWidth: 150
                                    editable: true
                                    model: ["1280x720", "1600x900", "1920x1080", "2560x1440", "3840x2160"]
                                    currentIndex: 2
                                }
                                Button {
                                    text: qsTr("Request resize")
                                    enabled: qsfClient.ready
                                    onClicked: {
                                        qsfClient.requestResizeText(guestResolution.editText)
                                    }
                                }
                                Item { Layout.fillWidth: true }
                                Label {
                                    text: qsTr("QSF asks the guest to resize and asks QEMU for SetUIInfo. Its reply confirms only that request; verify the resulting scanout in the stream trace.")
                                    Layout.maximumWidth: 430
                                    wrapMode: Text.Wrap
                                    opacity: 0.75
                                }
                            }
                        }
                    }

                    GroupBox {
                        title: qsTr("Constrained file transfer")
                        Layout.fillWidth: true

                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 10

                            Label {
                                Layout.fillWidth: true
                                text: qsTr("Files are independent QSF payloads, not Moonlight packets: 2 MiB maximum and safe ASCII basenames only. Uploads go to the guest inbox; downloads read its outbox.")
                                wrapMode: Text.Wrap
                            }

                            GridLayout {
                                columns: 3
                                Layout.fillWidth: true
                                columnSpacing: 10
                                rowSpacing: 8

                                Label { text: qsTr("Local upload path") }
                                TextField { id: uploadPath; Layout.fillWidth: true; placeholderText: "/local/path/file.txt" }
                                Button {
                                    text: qsTr("Upload")
                                    enabled: qsfClient.ready
                                    onClicked: qsfClient.uploadFile(uploadPath.text, uploadName.text)
                                }
                                Label { text: qsTr("Guest upload name") }
                                TextField { id: uploadName; Layout.fillWidth: true; placeholderText: qsTr("Optional basename") }
                                Item { }

                                Label { text: qsTr("Guest outbox name") }
                                TextField { id: downloadName; Layout.fillWidth: true; placeholderText: "guest-download.txt" }
                                Button {
                                    text: qsTr("Download")
                                    enabled: qsfClient.ready
                                    onClicked: qsfClient.downloadFile(downloadName.text, downloadPath.text)
                                }
                                Label { text: qsTr("Local download path") }
                                TextField { id: downloadPath; Layout.fillWidth: true; placeholderText: "/local/path/download.txt" }
                                Item { }
                            }
                        }
                    }

                    Label {
                        Layout.fillWidth: true
                        visible: qsfClient.lastError.length > 0
                        text: qsfClient.lastError
                        color: "#ef9a9a"
                        wrapMode: Text.Wrap
                    }
                    Label {
                        Layout.fillWidth: true
                        visible: qsfClient.lastResult.length > 0
                        text: qsfClient.lastResult
                        color: "#a5d6a7"
                        wrapMode: Text.Wrap
                    }
                    Label {
                        Layout.fillWidth: true
                        text: qsfClient.status
                        wrapMode: Text.Wrap
                    }
                }
            }
        }

        Item {
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 22
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
                    text: qsTr("Security boundary: QSF mTLS authenticates the companion gateway, but the gateway is launched per VM/session by the host. The current protocol does not cryptographically bind GameStream and QSF sessions; do not point a profile at an untrusted gateway.")
                }
            }
        }
    }
}
