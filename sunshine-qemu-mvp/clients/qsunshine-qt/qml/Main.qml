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

    // Keep every page pinned to the viewport's leading edge.  The previous
    // centered fixed-width columns acquired a negative x coordinate while a
    // native window was resized from its left edge, which made controls seem
    // to slide behind the window border.  At compact desktop widths grids
    // stack their label/control pairs instead of asking a Layout to overflow.
    readonly property int pageMargin: 22
    readonly property bool compactPageLayout: width < 1040

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
    }

    function saveDesktopProfile() {
        // A profile name is the authorization scope, not merely a cosmetic
        // label. Reusing it for a different media host must not retain a
        // system-auth admission that was issued for the previous endpoint.
        var previousMediaHost = moonlight.profileHost
        var saved = moonlight.saveProfile(profileNameField.text, hostField.text,
                                          appField.text, initialResolution.editText,
                                          displayMode.currentText)
        if (saved) {
            var mediaHostChanged = moonlight.profileHost !== previousMediaHost
            qsfClient.selectProfile(moonlight.currentProfileId)
            systemAuth.selectProfile(moonlight.currentProfileId)
            if (mediaHostChanged && systemAuth.authenticated)
                systemAuth.logout()
            root.loadDesktopFields()
            root.loadQsfFields()
            root.loadSystemAuthFields()
        }
        return saved
    }

    function loadSystemAuthFields() {
        systemAuthHostField.text = systemAuth.host
        systemAuthPortField.text = systemAuth.port > 0 ? String(systemAuth.port) : "48123"
        systemAuthServerNameField.text = systemAuth.serverName
        systemAuthCaField.text = systemAuth.caFile
        systemAuthAudienceField.text = systemAuth.expectedAudience
    }

    function saveSystemAuthFields() {
        return systemAuth.applyConfigurationText(systemAuthHostField.text,
                                                  systemAuthPortField.text,
                                                  systemAuthServerNameField.text,
                                                  systemAuthCaField.text,
                                                  systemAuthAudienceField.text)
    }

    function submitSystemLogin() {
        if (!root.saveSystemAuthFields())
            return
        systemAuth.login(systemUsernameField.text, systemPasswordField.text)
        // Clear the QML editor immediately after its one use. The backend
        // keeps its serialized request only until QSslSocket accepts it.
        systemPasswordField.clear()
    }

    function loadQsfFields() {
        qsfHostField.text = qsfClient.host
        qsfPortField.text = qsfClient.port > 0 ? String(qsfClient.port) : "48122"
        qsfServerNameField.text = qsfClient.serverName
        qsfCaField.text = qsfClient.caFile
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
                                                "", "")
    }

    Component.onCompleted: {
        loadDesktopFields()
        qsfClient.selectProfile(moonlight.currentProfileId)
        systemAuth.selectProfile(moonlight.currentProfileId)
        loadQsfFields()
        loadSystemAuthFields()
    }

    Connections {
        target: moonlight
        function onProfileChanged() {
            root.loadDesktopFields()
            qsfClient.selectProfile(moonlight.currentProfileId)
            systemAuth.selectProfile(moonlight.currentProfileId)
            root.loadQsfFields()
            root.loadSystemAuthFields()
        }
        function onProfilesChanged() { root.loadDesktopFields() }
    }

    Connections {
        target: qsfClient
        function onProfileChanged() { root.loadQsfFields() }
        function onConfigurationChanged() { root.loadQsfFields() }
        function onConnectionProfileReceived(width, height, fps, bitrateKbps, videoCodec, qemuApplied) {
            guestResolution.editText = String(width) + "x" + String(height)
        }
    }

    Connections {
        target: systemAuth
        function onProfileChanged() { root.loadSystemAuthFields() }
        function onConfigurationChanged() { root.loadSystemAuthFields() }
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
                Layout.minimumWidth: 0
                elide: Text.ElideRight
            }
            Label {
                text: profileNegotiation.busy ? qsTr("Applying display profile") :
                      (qsfClient.ready ? qsTr("QSF ready") :
                       (qsfClient.sessionActive ? qsTr("QSF verifying") : qsTr("QSF inactive")))
                color: qsfClient.ready ? Material.accent : "#ffb74d"
                Layout.maximumWidth: 140
                Layout.minimumWidth: 0
                elide: Text.ElideRight
            }
            Label {
                text: systemAuth.authenticated
                      ? qsTr("Signed in: %1").arg(systemAuth.subject)
                      : (systemAuth.authenticating ? qsTr("Signing in") : qsTr("Sign in required"))
                color: systemAuth.authenticated ? Material.accent : "#ffb74d"
                Layout.maximumWidth: 210
                elide: Text.ElideRight
            }
        }
    }

    TabBar {
        id: tabs
        objectName: "mainTabs"
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

                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        text: qsTr("q-sunshine uses a short-lived system sign-in for this desktop profile. The patched Moonlight media process receives an in-memory, VM-bound lease after sign-in; PIN pairing is not available.")
                    }

                    GroupBox {
                        title: qsTr("Desktop profile")
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0

                        GridLayout {
                            columns: root.compactPageLayout ? 1 : 2
                            anchors.fill: parent
                            columnSpacing: 12
                            rowSpacing: 10

                            Label { text: qsTr("Saved profile") }
                            RowLayout {
                                Layout.fillWidth: true
                                ComboBox {
                                    id: profileSelector
                                    Layout.preferredWidth: 210
                                    enabled: !profileNegotiation.busy && !moonlight.streamBusy && !moonlight.pairing
                                    model: moonlight.profileIds
                                    currentIndex: {
                                        var index = moonlight.profileIds.indexOf(moonlight.currentProfileId)
                                        return index < 0 ? 0 : index
                                    }
                                    onActivated: {
                                        if (moonlight.selectProfile(currentText)) {
                                            root.loadDesktopFields()
                                            qsfClient.selectProfile(moonlight.currentProfileId)
                                            systemAuth.selectProfile(moonlight.currentProfileId)
                                            root.loadQsfFields()
                                            root.loadSystemAuthFields()
                                        } else {
                                            root.loadDesktopFields()
                                        }
                                    }
                                }
                                TextField {
                                    id: profileNameField
                                    Layout.fillWidth: true
                                    enabled: !profileNegotiation.busy && !moonlight.streamBusy && !moonlight.pairing
                                    placeholderText: qsTr("Profile name; saving a new name creates a profile")
                                    maximumLength: 64
                                }
                                Button {
                                    text: qsTr("Save profile")
                                    enabled: !profileNegotiation.busy && !moonlight.streamBusy && !moonlight.pairing
                                    onClicked: root.saveDesktopProfile()
                                }
                            }

                            Label { text: qsTr("Sunshine host") }
                            TextField {
                                id: hostField
                                Layout.fillWidth: true
                                enabled: !profileNegotiation.busy && !moonlight.streamBusy && !moonlight.pairing
                                placeholderText: "192.168.64.25"
                            }

                            Label { text: qsTr("Application") }
                            TextField {
                                id: appField
                                Layout.fillWidth: true
                                enabled: !profileNegotiation.busy && !moonlight.pairing && (moonlight.running || !moonlight.streamBusy)
                                // App, stream size, and presentation deliberately remain editable
                                // during a session: Connect performs a controlled reconnect.
                            }

                            Label { text: qsTr("Initial stream size") }
                            ComboBox {
                                id: initialResolution
                                Layout.fillWidth: true
                                enabled: !profileNegotiation.busy && !moonlight.pairing && (moonlight.running || !moonlight.streamBusy)
                                editable: true
                                model: ["1280x720", "1600x900", "1920x1080", "2560x1440", "3840x2160"]
                            }

                            Label { text: qsTr("Presentation") }
                            ComboBox {
                                id: displayMode
                                objectName: "presentationMode"
                                Layout.fillWidth: true
                                enabled: !profileNegotiation.busy && !moonlight.pairing && (moonlight.running || !moonlight.streamBusy)
                                model: ["windowed", "fullscreen"]
                            }

                            Label { text: qsTr("Video decoder") }
                            ComboBox {
                                id: videoDecoder
                                Layout.fillWidth: true
                                enabled: !profileNegotiation.busy && !moonlight.streamBusy && !moonlight.pairing
                                model: ["auto", "software", "hardware"]
                                onActivated: {
                                    moonlight.videoDecoder = currentValue
                                    currentIndex = indexOfValue(moonlight.videoDecoder)
                                }
                            }

                        }
                    }

                    GroupBox {
                        title: qsTr("System authentication")
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0

                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 10

                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                text: qsTr("Sign in with an account authorized for this VM. The password is sent once over verified TLS to the host PAM service; it is never saved in the desktop profile.")
                            }

                            GridLayout {
                                columns: root.compactPageLayout ? 1 : 2
                                Layout.fillWidth: true
                                enabled: !profileNegotiation.busy && !moonlight.streamBusy &&
                                         !systemAuth.authenticating
                                columnSpacing: 12
                                rowSpacing: 8

                                Label { text: qsTr("Auth gateway host") }
                                TextField {
                                    id: systemAuthHostField
                                    Layout.fillWidth: true
                                    placeholderText: "auth.example"
                                }
                                Label { text: qsTr("TCP port") }
                                TextField {
                                    id: systemAuthPortField
                                    Layout.fillWidth: true
                                    inputMethodHints: Qt.ImhDigitsOnly
                                }
                                Label { text: qsTr("TLS server name") }
                                TextField {
                                    id: systemAuthServerNameField
                                    Layout.fillWidth: true
                                    placeholderText: qsTr("Optional; defaults to gateway host")
                                }
                                Label { text: qsTr("Gateway CA PEM") }
                                TextField {
                                    id: systemAuthCaField
                                    Layout.fillWidth: true
                                    placeholderText: "/path/to/auth-server-ca.crt"
                                }
                                Label { text: qsTr("Expected VM audience") }
                                TextField {
                                    id: systemAuthAudienceField
                                    objectName: "systemAuthAudience"
                                    Layout.fillWidth: true
                                    maximumLength: 128
                                    placeholderText: "vm-100"
                                    inputMethodHints: Qt.ImhNoPredictiveText
                                }
                                Label { text: qsTr("System login") }
                                TextField {
                                    id: systemUsernameField
                                    objectName: "systemAuthUsername"
                                    Layout.fillWidth: true
                                    maximumLength: 64
                                    inputMethodHints: Qt.ImhNoPredictiveText
                                }
                                Label { text: qsTr("Password") }
                                TextField {
                                    id: systemPasswordField
                                    objectName: "systemAuthPassword"
                                    Layout.fillWidth: true
                                    echoMode: TextInput.Password
                                    inputMethodHints: Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
                                    onAccepted: root.submitSystemLogin()
                                }
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                Button {
                                    text: qsTr("Save endpoint")
                                    enabled: !profileNegotiation.busy && !moonlight.streamBusy &&
                                             !systemAuth.authenticating
                                    onClicked: root.saveSystemAuthFields()
                                }
                                Button {
                                    id: systemLoginButton
                                    objectName: "systemAuthLogin"
                                    text: systemAuth.authenticating ? qsTr("Signing in") : qsTr("Sign in")
                                    highlighted: true
                                    enabled: !profileNegotiation.busy && !moonlight.streamBusy &&
                                             !systemAuth.authenticating
                                    onClicked: root.submitSystemLogin()
                                }
                                Button {
                                    text: qsTr("Sign out")
                                    enabled: systemAuth.authenticated || systemAuth.authenticating
                                    onClicked: systemAuth.logout()
                                }
                                Item { Layout.fillWidth: true }
                                Label {
                                    text: systemAuth.authenticated
                                          ? qsTr("Session expires at %1 UTC").arg(
                                                systemAuth.expiresAtUtc.toString("HH:mm:ss"))
                                          : qsTr("A current sign-in is required before connecting")
                                    opacity: 0.72
                                    wrapMode: Text.Wrap
                                    Layout.maximumWidth: 330
                                }
                            }

                            Label {
                                Layout.fillWidth: true
                                visible: systemAuth.lastError.length > 0
                                text: systemAuth.lastError
                                color: "#ef9a9a"
                                wrapMode: Text.Wrap
                            }
                            Label {
                                Layout.fillWidth: true
                                text: systemAuth.status
                                opacity: 0.75
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
                            enabled: systemAuth.authenticated && moonlight.canStartStream &&
                                     !profileNegotiation.busy && !moonlight.pairing &&
                                     (moonlight.running || !moonlight.streamBusy)
                            onClicked: {
                                if (!root.saveDesktopProfile())
                                    return
                                moonlight.startStream(moonlight.profileHost, moonlight.profileAppName,
                                                      moonlight.profileResolution,
                                                      moonlight.profileDisplayMode)
                            }
                        }
                        Button {
                            text: qsTr("Disconnect")
                            enabled: !profileNegotiation.busy && moonlight.streamBusy
                            onClicked: moonlight.stopStream()
                        }
                        Button {
                            text: qsTr("Cancel profile handoff")
                            visible: profileNegotiation.busy
                            enabled: profileNegotiation.busy
                            onClicked: profileNegotiation.cancel()
                        }
                        Item { Layout.fillWidth: true }
                        Label {
                            text: profileNegotiation.busy
                                  ? qsTr("The guest scanout handoff owns this session. A cancellation waits for any in-flight guest transaction to reach a safe terminal state before another connection change.")
                                  : qsTr("Changing the app, presentation, or initial stream size while connected performs a controlled graphics-stream reconnect. Guest resize is separate in the companion tab.")
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
                        title: qsTr("QSF TLS 1.3 companion for this desktop profile")
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0

                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 10

                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                text: qsTr("The gateway verifies the current short-lived system-auth ticket and reads the host-local QSF token itself. The host token, user password, and session ticket never persist in this profile.")
                            }

                            GridLayout {
                                columns: root.compactPageLayout ? 1 : 2
                                Layout.fillWidth: true
                                enabled: !profileNegotiation.busy && !qsfClient.sessionActive
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
                            }

                            RowLayout {
                                Button {
                                    text: qsTr("Save")
                                    enabled: !profileNegotiation.busy && !qsfClient.sessionActive
                                    onClicked: root.saveQsfFields()
                                }
                                Button {
                                    text: qsTr("Test gateway")
                                    enabled: systemAuth.authenticated && !profileNegotiation.busy &&
                                             !qsfClient.sessionActive
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
                        Layout.minimumWidth: 0

                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 8

                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                    text: qsTr("Activate QSF only after the graphics window visibly shows the selected desktop. A child process starting is not treated as proof of a visible session, and no log parsing is used as an authority signal.")
                            }
                            RowLayout {
                                Button {
                                    text: qsfClient.sessionActive ? qsTr("Deactivate QSF companion") : qsTr("Activate QSF for visible stream")
                                    enabled: systemAuth.authenticated && !profileNegotiation.busy &&
                                             ((moonlight.running && !moonlight.streamStopping) ||
                                              qsfClient.sessionActive)
                                    onClicked: qsfClient.sessionActive = !qsfClient.sessionActive
                                }
                                Label {
                                    Layout.fillWidth: true
                                    wrapMode: Text.Wrap
                                    opacity: 0.75
                                    text: qsfClient.sessionActive
                                          ? qsTr("The profile is being verified with its system-authenticated gateway; ending or reconnecting graphics cancels every QSF operation.")
                                          : qsTr("Inactive profiles cannot send clipboard, resize, upload, or download operations.")
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
                                Button {
                                    text: qsTr("Choose optimal stream profile")
                                    highlighted: true
                                    enabled: qsfClient.ready && moonlight.running &&
                                             !moonlight.streamStopping && !profileNegotiation.busy
                                    onClicked: {
                                        if (root.saveDesktopProfile())
                                            profileNegotiation.negotiate(
                                                        moonlight.profileResolution,
                                                        moonlight.videoDecoder)
                                    }
                                }
                                Label {
                                    Layout.fillWidth: true
                                    wrapMode: Text.Wrap
                                    opacity: 0.75
                                    text: qsTr("The size selected in this client is the requested VirGL guest scanout. The client decoder, Sunshine encoder, and guest display envelope negotiate FPS, bitrate, and codec. The current Moonlight stream is stopped first; only after QEMU and the guest confirm the new scanout does Moonlight launch again. Activate QSF again after the new video is visibly shown.")
                                }
                            }
                            Label {
                                Layout.fillWidth: true
                                visible: moonlight.profileFps > 0
                                text: qsTr("Current stream profile: %1 · %2 FPS · %3 Kbps · %4")
                                      .arg(moonlight.profileResolution)
                                      .arg(moonlight.profileFps)
                                      .arg(moonlight.profileBitrateKbps)
                                      .arg(moonlight.profileVideoCodec)
                                opacity: 0.75
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
                                    Layout.preferredWidth: 170
                                    enabled: !profileNegotiation.busy && !qsfClient.sessionActive
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
                                    enabled: !profileNegotiation.busy
                                    editable: true
                                    model: ["1280x720", "1600x900", "1920x1080", "2560x1440", "3840x2160"]
                                    currentIndex: 2
                                }
                                Button {
                                    text: qsTr("Request resize")
                                    enabled: !profileNegotiation.busy && qsfClient.ready
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
                        Layout.minimumWidth: 0

                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 10

                            Label {
                                Layout.fillWidth: true
                                text: qsTr("Files are independent QSF payloads, not Moonlight packets: 2 MiB maximum and safe ASCII basenames only. Uploads go to the guest inbox; downloads read its outbox.")
                                wrapMode: Text.Wrap
                            }

                            GridLayout {
                                columns: root.compactPageLayout ? 1 : 3
                                Layout.fillWidth: true
                                columnSpacing: 10
                                rowSpacing: 8

                                Label { text: qsTr("Local upload path") }
                                TextField { id: uploadPath; Layout.fillWidth: true; enabled: !profileNegotiation.busy; placeholderText: "/local/path/file.txt" }
                                Button {
                                    text: qsTr("Upload")
                                    enabled: !profileNegotiation.busy && qsfClient.ready
                                    onClicked: qsfClient.uploadFile(uploadPath.text, uploadName.text)
                                }
                                Label { text: qsTr("Guest upload name") }
                                TextField { id: uploadName; Layout.fillWidth: true; enabled: !profileNegotiation.busy; placeholderText: qsTr("Optional basename") }
                                Item { }

                                Label { text: qsTr("Guest outbox name") }
                                TextField { id: downloadName; Layout.fillWidth: true; enabled: !profileNegotiation.busy; placeholderText: "guest-download.txt" }
                                Button {
                                    text: qsTr("Download")
                                    enabled: !profileNegotiation.busy && qsfClient.ready
                                    onClicked: qsfClient.downloadFile(downloadName.text, downloadPath.text)
                                }
                                Label { text: qsTr("Local download path") }
                                TextField { id: downloadPath; Layout.fillWidth: true; enabled: !profileNegotiation.busy; placeholderText: "/local/path/download.txt" }
                                Item { }
                            }
                        }
                    }

                    Label {
                        Layout.fillWidth: true
                        visible: profileNegotiation.lastError.length > 0
                        text: profileNegotiation.lastError
                        color: "#ef9a9a"
                        wrapMode: Text.Wrap
                    }
                    Label {
                        Layout.fillWidth: true
                        visible: profileNegotiation.busy
                        text: profileNegotiation.status
                        wrapMode: Text.Wrap
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
                    text: qsTr("Security boundary: system authentication issues short-lived VM-bound credentials for both QSF and GameStream. The media lease is held only in memory and is passed to the patched Moonlight process through a one-shot pipe; do not point a profile at an untrusted gateway.")
                }
            }
        }
    }
}
