// SPDX-License-Identifier: GPL-3.0-or-later
// Real-runtime orchestrator for the same Qt shell classes used by the GUI.
//
// The surrounding shell harness owns QEMU, Sunshine and the disposable client
// display. It creates activate-windowed only after it has verified the first
// Moonlight window, and activate-fullscreen only after it has verified the
// controlled reconnect. This preserves the product's explicit post-visible-
// video QSF activation boundary while making it automatable.

#include "moonlightcontroller.h"
#include "profilenegotiationcoordinator.h"
#include "qsfclient.h"
#include "systemauthclient.h"

#include <QClipboard>
#include <QCommandLineParser>
#include <QDir>
#include <QFile>
#include <QFileDevice>
#include <QFileInfo>
#include <QGuiApplication>
#include <QRegularExpression>
#include <QSaveFile>
#include <QStandardPaths>
#include <QTextStream>
#include <QTimer>

#include <cstdio>
#include <utility>

namespace {

struct Arguments {
    // Descriptor mode is the PVE Console path. It deliberately has no PVE
    // username, password, direct media route, QSF route, SNI, or CA option:
    // all of those arrive only after a one-use .qsm claim is redeemed.
    bool launchDescriptorMode = false;
    bool descriptorBootstrapOnly = false;
    QString launchFile;
    QString moonlightBinary;
    QString host;
    QString appName;
    QString initialResolution;
    QString fullscreenResolution;
    QString systemAuthHost;
    int systemAuthPort = 0;
    QString systemAuthServerName;
    QString systemAuthCaFile;
    QString systemAuthAudience;
    QString systemAuthUsername;
    // The real harness supplies this through stdin, never argv or an
    // environment variable.  It is moved into SystemAuthClient immediately
    // before the TLS/PAM login then scrubbed from this driver object.
    QString systemAuthPassword;
    QString qsfHost;
    int qsfPort = 0;
    QString qsfServerName;
    QString qsfCaFile;
    QString expectedGuestClipboardFile;
    QString clientClipboardFile;
    QString expectedGuestClipboard;
    QString clientClipboard;
    QString uploadSource;
    QString uploadName;
    QString downloadName;
    QString downloadDestination;
    QString fullscreenDownloadDestination;
    QString expectedDownloadSource;
    QString receivedClipboardDestination;
    int resizeWidth = 0;
    int resizeHeight = 0;
    int expectedNegotiatedFps = 0;
    int expectedNegotiatedBitrateKbps = 0;
    QString expectedNegotiatedVideoCodec;
    int expectedFullscreenFps = 0;
    int expectedFullscreenBitrateKbps = 0;
    QString expectedFullscreenVideoCodec;
    int timeoutMs = 300000;
    QString phaseDirectory;
    QString moonlightLog;
};

bool parseResolution(const QString& value, int* width, int* height)
{
    static const QRegularExpression pattern(QStringLiteral("\\A([0-9]{2,5})x([0-9]{2,5})\\z"));
    const QRegularExpressionMatch match = pattern.match(value.trimmed());
    if (!match.hasMatch()) {
        return false;
    }
    bool widthOk = false;
    bool heightOk = false;
    const int parsedWidth = match.captured(1).toInt(&widthOk);
    const int parsedHeight = match.captured(2).toInt(&heightOk);
    if (!widthOk || !heightOk || parsedWidth < 64 || parsedWidth > 16384 ||
        parsedHeight < 64 || parsedHeight > 16384) {
        return false;
    }
    *width = parsedWidth;
    *height = parsedHeight;
    return true;
}

bool writeAtomically(const QString& path, const QByteArray& contents)
{
    QSaveFile file(path);
    if (!file.open(QIODevice::WriteOnly)) {
        return false;
    }
    return file.write(contents) == contents.size() && file.commit();
}

bool filesEqual(const QString& expectedPath, const QString& actualPath)
{
    QFile expected(expectedPath);
    QFile actual(actualPath);
    if (!expected.open(QIODevice::ReadOnly) || !actual.open(QIODevice::ReadOnly)) {
        return false;
    }
    return expected.readAll() == actual.readAll();
}

bool readUtf8ClipboardFile(const QString& path, QString* text)
{
    constexpr qint64 kMaximumBytes = 1024 * 1024;
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        return false;
    }
    const QByteArray bytes = file.read(kMaximumBytes + 1);
    if (file.error() != QFileDevice::NoError || !file.atEnd() ||
        bytes.size() > kMaximumBytes || bytes.contains('\0')) {
        return false;
    }
    const QString decoded = QString::fromUtf8(bytes.constData(), bytes.size());
    if (decoded.toUtf8() != bytes) {
        return false;
    }
    *text = decoded;
    return true;
}

bool readSystemAuthPassword(QString* password)
{
    // This executable is a non-interactive harness.  stdin is an anonymous
    // pipe whose writer is the hook; accepting a single bounded UTF-8 line
    // avoids a credential in argv, environment, QSettings, or a temporary
    // pathname.  Moonlight's own stdin is a distinct managed QProcess pipe.
    constexpr qint64 kMaximumPasswordBytes = 4096;
    QFile input;
    if (!input.open(stdin, QIODevice::ReadOnly)) {
        return false;
    }
    QByteArray bytes = input.read(kMaximumPasswordBytes + 2);
    if (input.error() != QFileDevice::NoError || bytes.size() > kMaximumPasswordBytes + 1 ||
        (!input.atEnd() && bytes.size() == kMaximumPasswordBytes + 2)) {
        bytes.fill('\0');
        return false;
    }
    if (bytes.endsWith('\n')) {
        bytes.chop(1);
    }
    if (bytes.endsWith('\r') || bytes.isEmpty() || bytes.contains('\0') ||
        bytes.contains('\n') || bytes.size() > kMaximumPasswordBytes) {
        bytes.fill('\0');
        return false;
    }
    const QString decoded = QString::fromUtf8(bytes.constData(), bytes.size());
    const bool validUtf8 = decoded.toUtf8() == bytes;
    bytes.fill('\0');
    if (!validUtf8 || decoded.isEmpty() || decoded.toUtf8().size() > kMaximumPasswordBytes) {
        return false;
    }
    *password = decoded;
    return true;
}

class RealE2eDriver final : public QObject
{
public:
    RealE2eDriver(QGuiApplication& application, Arguments arguments)
        : QObject(&application),
          m_Application(application),
          m_Arguments(std::move(arguments)),
          m_Qsf(this),
          m_Moonlight(this),
          m_SystemAuth(this),
          m_ProfileNegotiation(&m_Qsf, &m_Moonlight, this),
          m_Clipboard(QGuiApplication::clipboard())
    {
        // This is the same composition as the desktop application's entry
        // point. Descriptor mode accepts only a PVE-issued one-use launch
        // file; the broker installs the VM-scoped media/QSF/lease routes in
        // process after TLS redemption. The retained legacy branch exists
        // only for its separately invoked historical system-auth fixture.
        // Neither path has a pairing/PIN fallback.
        m_Moonlight.requireSystemAuthGameStreamLease();
        m_SystemAuth.attachQsfClient(&m_Qsf);
        if (m_Arguments.launchDescriptorMode) {
            m_SystemAuth.setBrokerMediaRouteSink(
                [this](const QString& host, int basePort) {
                    return m_Moonlight.setSystemAuthGameStreamMediaRoute(host, basePort);
                },
                [this]() { m_Moonlight.clearSystemAuthGameStreamLease(); });
            m_SystemAuth.setBrokerGameStreamLeaseSink(
                [this](const QString& authHost, int authPort, const QString& authServerName,
                       const QByteArray& authCaPem, const QString& audience,
                       const QByteArray& ticket, qint64 expiresAtUtcMs) {
                    return m_Moonlight.setSystemAuthGameStreamLeasePem(
                        authHost, authPort, authServerName, authCaPem, audience, ticket,
                        expiresAtUtcMs);
                });
        }
        else {
            m_SystemAuth.setGameStreamLeaseSink(
                [this](const QString& authHost, int authPort, const QString& authServerName,
                       const QString& authCaFile, const QString& audience,
                       const QByteArray& ticket, qint64 expiresAtUtcMs) {
                    m_Moonlight.setSystemAuthGameStreamLease(
                        authHost, authPort, authServerName, authCaFile, audience, ticket,
                        expiresAtUtcMs);
                },
                [this]() { m_Moonlight.clearSystemAuthGameStreamLease(); });
        }
        m_PhasePoll.setInterval(100);
        connect(&m_PhasePoll, &QTimer::timeout, this, [this]() { pollHarnessPhases(); });

        m_Timeout.setSingleShot(true);
        m_Timeout.setInterval(m_Arguments.timeoutMs);
        connect(&m_Timeout, &QTimer::timeout, this, [this]() {
            fail(QStringLiteral("timed out waiting for real Qt/Moonlight/QSF E2E phases"));
        });

        connect(&m_Moonlight, &MoonlightController::streamStarted,
                this, [this]() { onStreamStarted(); });
        connect(&m_Moonlight, &MoonlightController::streamTeardownStarted,
                this, [this]() {
                    m_Qsf.setSessionActive(false);
                    const QString marker = m_Phase == Phase::QuiescingNegotiated
                        ? QStringLiteral("qsf-deactivated-for-negotiated-profile")
                        : (m_Phase == Phase::QuiescingFullscreen
                               ? QStringLiteral("qsf-deactivated-for-fullscreen-profile")
                               : (m_Phase == Phase::Stopping
                                      ? QStringLiteral("qsf-deactivated-for-stop")
                                      : QStringLiteral("qsf-deactivated")));
                    if (!writePhase(marker)) {
                        return;
                    }
                });
        connect(&m_Moonlight, &MoonlightController::streamFinished,
                this, [this]() { onStreamFinished(); });
        connect(&m_Moonlight, &MoonlightController::recentOutputChanged,
                this, [this]() { writeMoonlightLog(); });
        connect(&m_Moonlight, &MoonlightController::lastErrorChanged, this, [this]() {
            if (!m_Moonlight.lastError().isEmpty()) {
                fail(QStringLiteral("Moonlight controller: %1").arg(m_Moonlight.lastError()));
            }
        });

        connect(&m_Qsf, &QsfClient::readyChanged, this, [this]() {
            if (m_Qsf.ready()) {
                onQsfReady();
            }
        });
        connect(&m_Qsf, &QsfClient::clipboardReceivedFromGuest,
                this, [this]() { onClipboardReceived(); });
        connect(&m_Qsf, &QsfClient::clipboardSentToGuest,
                this, [this]() { onClipboardSent(); });
        connect(&m_Qsf, &QsfClient::fileTransferFinished,
                this, [this](const QString& description) { onFileTransfer(description); });
        connect(&m_Qsf, &QsfClient::connectionProfileReceived,
                this, [this](int width, int height, int fps, int bitrateKbps,
                             const QString& videoCodec, bool qemuApplied) {
                    onConnectionProfileReceived(width, height, fps, bitrateKbps,
                                                videoCodec, qemuApplied);
                });
        connect(&m_Qsf, &QsfClient::lastErrorChanged, this, [this]() {
            if (!m_Qsf.lastError().isEmpty()) {
                fail(QStringLiteral("QSF client: %1").arg(m_Qsf.lastError()));
            }
        });
        connect(&m_ProfileNegotiation, &ProfileNegotiationCoordinator::lastErrorChanged,
                this, [this]() {
                    if (!m_ProfileNegotiation.lastError().isEmpty()) {
                        fail(QStringLiteral("profile coordinator: %1")
                                 .arg(m_ProfileNegotiation.lastError()));
                    }
                });
        connect(&m_SystemAuth, &SystemAuthClient::authenticatedChanged, this, [this]() {
            m_Moonlight.setSystemAuthAdmission(m_SystemAuth.authenticated());
            if (m_Phase == Phase::Authenticating && m_SystemAuth.authenticated()) {
                if (!writePhase(m_Arguments.launchDescriptorMode
                                    ? QStringLiteral("descriptor-redeemed")
                                    : QStringLiteral("system-authenticated"))) {
                    return;
                }
                startWindowedStream();
            }
        });
        connect(&m_SystemAuth, &SystemAuthClient::lastErrorChanged, this, [this]() {
            if (!m_SystemAuth.lastError().isEmpty()) {
                fail(QStringLiteral("System authentication: %1").arg(m_SystemAuth.lastError()));
            }
        });
        connect(&m_Application, &QGuiApplication::aboutToQuit, this, [this]() {
            m_Qsf.setSessionActive(false);
            m_SystemAuth.logout();
            m_Moonlight.stopStream();
        });
    }

    void start()
    {
        const QDir phaseDirectory(m_Arguments.phaseDirectory);
        if (!QFileInfo(m_Arguments.phaseDirectory).isDir() ||
            !phaseDirectory.entryList(QDir::AllEntries | QDir::NoDotAndDotDot).isEmpty()) {
            fail(QStringLiteral("real E2E requires a fresh private phase directory"));
            return;
        }
        if (m_Arguments.launchDescriptorMode && !QFileInfo(m_Arguments.launchFile).isFile()) {
            fail(QStringLiteral("descriptor E2E launch file is unavailable"));
            return;
        }
        if (!m_Arguments.descriptorBootstrapOnly) {
            if (m_Clipboard == nullptr) {
                fail(QStringLiteral("Qt has no clipboard implementation on this platform"));
                return;
            }
            if (!QFileInfo(m_Arguments.uploadSource).isFile() ||
                !QFileInfo(m_Arguments.expectedGuestClipboardFile).isFile() ||
                !QFileInfo(m_Arguments.clientClipboardFile).isFile() ||
                !QFileInfo(m_Arguments.expectedDownloadSource).isFile() ||
                (!m_Arguments.launchDescriptorMode &&
                 (!QFileInfo(m_Arguments.systemAuthCaFile).isFile() ||
                  !QFileInfo(m_Arguments.qsfCaFile).isFile()))) {
                fail(QStringLiteral("real E2E has a missing fixture or TLS credential"));
                return;
            }
            if (!QFileInfo(m_Arguments.receivedClipboardDestination).dir().exists() ||
                QFileInfo(m_Arguments.receivedClipboardDestination).exists() ||
                !QFileInfo(m_Arguments.downloadDestination).dir().exists() ||
                QFileInfo(m_Arguments.downloadDestination).exists() ||
                !QFileInfo(m_Arguments.fullscreenDownloadDestination).dir().exists() ||
                QFileInfo(m_Arguments.fullscreenDownloadDestination).exists() ||
                !readUtf8ClipboardFile(m_Arguments.expectedGuestClipboardFile,
                                       &m_Arguments.expectedGuestClipboard) ||
                !readUtf8ClipboardFile(m_Arguments.clientClipboardFile,
                                       &m_Arguments.clientClipboard)) {
                fail(QStringLiteral("real E2E has an invalid fixture or stale result path"));
                return;
            }
            if (m_Arguments.clientClipboard.isEmpty() ||
                m_Arguments.expectedGuestClipboard.isEmpty() ||
                m_Arguments.clientClipboard == m_Arguments.expectedGuestClipboard) {
                fail(QStringLiteral("real E2E requires distinct non-empty client and guest clipboard fixtures"));
                return;
            }
        }
        if (!m_Arguments.moonlightLog.isEmpty() &&
            !QFileInfo(m_Arguments.moonlightLog).dir().exists()) {
            fail(QStringLiteral("Moonlight diagnostic log parent does not exist"));
            return;
        }
        if (!m_Moonlight.setTestMoonlightBinary(m_Arguments.moonlightBinary)) {
            fail(QStringLiteral("could not select the real-E2E Moonlight test child: %1")
                     .arg(m_Moonlight.lastError()));
            return;
        }
        // Xvfb cannot read back an NVIDIA VDPAU presentation surface with
        // xwd. Force the production controller's constrained software choice
        // only in this disposable visual-attestation driver, so the non-black
        // screenshot proves decoded pixels rather than an overlay placeholder.
        m_Moonlight.setVideoDecoder(QStringLiteral("software"));
        if (m_Moonlight.videoDecoder() != QStringLiteral("software")) {
            fail(QStringLiteral("could not select the Moonlight software decoder for visual attestation"));
            return;
        }
        const QString profileHost = m_Arguments.launchDescriptorMode
            ? QStringLiteral("terminal-broker") : m_Arguments.host;
        if (!m_Moonlight.saveProfile(QStringLiteral("real-qt-e2e"), profileHost,
                                     m_Arguments.appName, m_Arguments.initialResolution,
                                     QStringLiteral("windowed"))) {
            fail(QStringLiteral("could not save the real E2E desktop profile: %1")
                     .arg(m_Moonlight.lastError()));
            return;
        }
        m_Qsf.selectProfile(m_Moonlight.currentProfileId());
        if (!m_Arguments.launchDescriptorMode) {
            if (!m_Qsf.applyConfiguration(m_Arguments.qsfHost, m_Arguments.qsfPort,
                                          m_Arguments.qsfServerName, m_Arguments.qsfCaFile,
                                          QString(), QString())) {
                fail(QStringLiteral("could not configure QSF: %1").arg(m_Qsf.lastError()));
                return;
            }
            if (!m_SystemAuth.selectProfile(m_Moonlight.currentProfileId()) ||
                !m_SystemAuth.applyConfiguration(m_Arguments.systemAuthHost,
                                                 m_Arguments.systemAuthPort,
                                                 m_Arguments.systemAuthServerName,
                                                 m_Arguments.systemAuthCaFile,
                                                 m_Arguments.systemAuthAudience)) {
                fail(QStringLiteral("could not configure system authentication: %1")
                         .arg(m_SystemAuth.lastError()));
                return;
            }
        }

        if (!m_Arguments.descriptorBootstrapOnly) {
            // The guest's native-Wayland fixture intentionally waits for this
            // client value before it creates its independent guest-side copy.
            // This exercises both directions in a causally unambiguous order.
            m_Qsf.setInitialClipboardDirection(QStringLiteral("client"));
            m_Qsf.setClipboardSyncEnabled(true);
            m_Clipboard->setText(m_Arguments.clientClipboard, QClipboard::Clipboard);
        }

        if (!writePhase(QStringLiteral("driver-ready"))) {
            return;
        }
        m_Phase = Phase::Authenticating;
        m_Timeout.start();
        m_PhasePoll.start();
        if (m_Arguments.launchDescriptorMode) {
            if (!writePhase(QStringLiteral("descriptor-redeem-started"))) {
                return;
            }
            if (!m_SystemAuth.claimLaunchFile(m_Arguments.launchFile)) {
                fail(QStringLiteral("could not redeem descriptor launch file: %1")
                         .arg(m_SystemAuth.lastError()));
            }
            return;
        }
        if (!writePhase(QStringLiteral("system-auth-login-started"))) {
            return;
        }
        // SystemAuthClient serializes the TLS/PAM request synchronously, then
        // retains only its short-lived ticket.  Clear this driver's final
        // password copy immediately after that hand-off.
        QString password = std::move(m_Arguments.systemAuthPassword);
        m_Arguments.systemAuthPassword.fill(QChar::Null);
        m_Arguments.systemAuthPassword.clear();
        m_SystemAuth.login(m_Arguments.systemAuthUsername, password);
        password.fill(QChar::Null);
    }

private:
    enum class Phase {
        Authenticating,
        StartingWindowed,
        AwaitWindowedVerification,
        AwaitWindowedQsfReady,
        AwaitInitialClientClipboardAck,
        AwaitGuestClipboard,
        AwaitDataOperations,
        QuiescingNegotiated,
        AwaitNegotiatedProfileQsfReady,
        AwaitNegotiatedProfile,
        LaunchingNegotiated,
        AwaitNegotiatedVerification,
        AwaitNegotiatedQsfActivation,
        AwaitNegotiatedQsfReady,
        AwaitFullscreenProfileActivation,
        QuiescingFullscreen,
        AwaitFullscreenProfileQsfReady,
        AwaitFullscreenProfile,
        LaunchingFullscreen,
        AwaitFullscreenVerification,
        AwaitFullscreenQsfReady,
        AwaitFullscreenDownload,
        Stopping,
        Complete,
        Failed,
    };

    bool phaseRequested(const QString& name) const
    {
        return QFileInfo(QDir(m_Arguments.phaseDirectory).filePath(name)).isFile();
    }

    bool writePhase(const QString& name)
    {
        const QString path = QDir(m_Arguments.phaseDirectory).filePath(name);
        if (!writeAtomically(path, name.toUtf8() + '\n')) {
            fail(QStringLiteral("could not write E2E phase marker: %1").arg(name));
            return false;
        }
        return true;
    }

    bool writeMoonlightLog()
    {
        if (!m_Arguments.moonlightLog.isEmpty() &&
            !writeAtomically(m_Arguments.moonlightLog,
                             m_Moonlight.recentOutput().toUtf8())) {
            fail(QStringLiteral("could not retain redacted Moonlight diagnostics"));
            return false;
        }
        return true;
    }

    void startWindowedStream()
    {
        m_Phase = Phase::StartingWindowed;
        const QString profileHost = m_Arguments.launchDescriptorMode
            ? m_Moonlight.profileHost() : m_Arguments.host;
        m_Moonlight.startStream(profileHost, m_Arguments.appName,
                                m_Arguments.initialResolution,
                                QStringLiteral("windowed"));
    }

    void pollHarnessPhases()
    {
        if (m_Phase == Phase::AwaitWindowedVerification &&
            phaseRequested(QStringLiteral("activate-windowed"))) {
            if (!writePhase(QStringLiteral("windowed-activation-accepted"))) {
                return;
            }
            m_Phase = Phase::AwaitWindowedQsfReady;
            m_Qsf.setSessionActive(true);
        }
        else if (m_Phase == Phase::AwaitNegotiatedVerification &&
                 phaseRequested(QStringLiteral("negotiated-video-verified"))) {
            if (!writePhase(QStringLiteral("negotiated-video-verification-accepted"))) {
                return;
            }
            // The replacement video is now visible, so model the explicit
            // product boundary again: activate QSF for this visible stream
            // before asking it to change the next fullscreen scanout.
            m_Phase = Phase::AwaitNegotiatedQsfActivation;
        }
        else if (m_Phase == Phase::AwaitNegotiatedQsfActivation &&
                 phaseRequested(QStringLiteral("activate-negotiated-qsf"))) {
            if (!writePhase(QStringLiteral("negotiated-qsf-activation-accepted"))) {
                return;
            }
            m_Phase = Phase::AwaitNegotiatedQsfReady;
            m_Qsf.setSessionActive(true);
        }
        else if (m_Phase == Phase::AwaitFullscreenVerification &&
            phaseRequested(QStringLiteral("activate-fullscreen"))) {
            if (!writePhase(QStringLiteral("fullscreen-activation-accepted"))) {
                return;
            }
            m_Phase = Phase::AwaitFullscreenQsfReady;
            m_Qsf.setSessionActive(true);
        }
        else if (m_Phase == Phase::AwaitFullscreenProfileActivation &&
                 phaseRequested(QStringLiteral("activate-fullscreen-profile"))) {
            // This mirrors Save profile in the production Qt UI. It records
            // the future fullscreen presentation, then the coordinator
            // retires the currently visible stream before QSF changes the
            // guest scanout.
            if (!m_Moonlight.saveProfile(m_Moonlight.currentProfileId(),
                                         m_Moonlight.profileHost(), m_Arguments.appName,
                                         m_Arguments.fullscreenResolution,
                                         QStringLiteral("fullscreen"))) {
                fail(QStringLiteral("could not select the requested fullscreen stream profile"));
                return;
            }
            if (!writePhase(QStringLiteral("fullscreen-profile-activation-accepted"))) {
                return;
            }
            m_Phase = Phase::QuiescingFullscreen;
            if (!m_ProfileNegotiation.negotiate(m_Arguments.fullscreenResolution,
                                                QStringLiteral("software"))) {
                fail(QStringLiteral("could not begin safe fullscreen profile negotiation: %1")
                         .arg(m_ProfileNegotiation.lastError()));
            }
        }
    }

    void onStreamStarted()
    {
        ++m_StreamStarts;
        if (m_StreamStarts == 1 && m_Phase == Phase::StartingWindowed) {
            if (!writePhase(QStringLiteral("windowed-process-started"))) {
                return;
            }
            if (m_Arguments.descriptorBootstrapOnly) {
                // The launch-file smoke deliberately stops before QSF is
                // activated: it proves only the real receiver composition
                // (strict descriptor redemption -> in-memory broker routes ->
                // lease-aware Moonlight child). Full data-plane E2E remains
                // gated behind visible-video activation markers below.
                if (!m_SystemAuth.authenticated() || !m_Qsf.configured()) {
                    fail(QStringLiteral("descriptor redemption did not install a usable in-memory route"));
                    return;
                }
                if (!writePhase(QStringLiteral("descriptor-bootstrap-verified"))) {
                    return;
                }
                m_Phase = Phase::Stopping;
                m_Moonlight.stopStream();
                return;
            }
            m_Phase = Phase::AwaitWindowedVerification;
            return;
        }
        if (m_StreamStarts == 2 && m_Phase == Phase::LaunchingNegotiated) {
            if (!writePhase(QStringLiteral("negotiated-process-started"))) {
                return;
            }
            m_Phase = Phase::AwaitNegotiatedVerification;
            return;
        }
        if (m_StreamStarts == 3 && m_Phase == Phase::LaunchingFullscreen) {
            if (!writePhase(QStringLiteral("fullscreen-process-started"))) {
                return;
            }
            m_Phase = Phase::AwaitFullscreenVerification;
            return;
        }
        fail(QStringLiteral("Moonlight started in an unexpected E2E phase"));
    }

    void onStreamFinished()
    {
        if (m_Phase == Phase::QuiescingNegotiated) {
            if (!writePhase(QStringLiteral("windowed-stream-quiesced"))) {
                return;
            }
            m_Phase = Phase::AwaitNegotiatedProfileQsfReady;
            return;
        }
        if (m_Phase == Phase::QuiescingFullscreen) {
            if (!writePhase(QStringLiteral("negotiated-stream-quiesced"))) {
                return;
            }
            m_Phase = Phase::AwaitFullscreenProfileQsfReady;
            return;
        }
        if (m_Phase == Phase::Stopping) {
            m_PhasePoll.stop();
            m_Timeout.stop();
            if (!writeMoonlightLog()) {
                return;
            }
            if (!writePhase(QStringLiteral("complete"))) {
                return;
            }
            m_Phase = Phase::Complete;
            const QString marker = m_Arguments.launchDescriptorMode
                ? (m_Arguments.descriptorBootstrapOnly
                       ? QStringLiteral("QSUNSHINE_QT_DESCRIPTOR_BOOTSTRAP_OK")
                       : QStringLiteral("QSUNSHINE_QT_DESCRIPTOR_E2E_OK"))
                : QStringLiteral("QSUNSHINE_QT_REAL_E2E_OK");
            QTextStream(stdout) << marker << '\n' << Qt::flush;
            QTimer::singleShot(0, &m_Application, [this]() { m_Application.exit(0); });
            return;
        }
        if (m_Phase != Phase::Failed && m_Phase != Phase::Complete) {
            fail(QStringLiteral("Moonlight child ended outside controlled teardown"));
        }
    }

    void onQsfReady()
    {
        if (m_Phase == Phase::AwaitWindowedQsfReady) {
            if (!writePhase(QStringLiteral("qsf-windowed-ready"))) {
                return;
            }
            m_Phase = Phase::AwaitInitialClientClipboardAck;
        }
        else if (m_Phase == Phase::AwaitNegotiatedProfileQsfReady) {
            if (!writePhase(QStringLiteral("qsf-negotiated-profile-ready"))) {
                return;
            }
            // ProfileNegotiationCoordinator receives readyChanged first and
            // queues the only permitted operation for this temporary lease.
            m_Phase = Phase::AwaitNegotiatedProfile;
        }
        else if (m_Phase == Phase::AwaitNegotiatedQsfReady) {
            if (!writePhase(QStringLiteral("qsf-negotiated-ready"))) {
                return;
            }
            m_Phase = Phase::AwaitFullscreenProfileActivation;
        }
        else if (m_Phase == Phase::AwaitFullscreenProfileQsfReady) {
            if (!writePhase(QStringLiteral("qsf-fullscreen-profile-ready"))) {
                return;
            }
            // As above, the production coordinator owns the actual request.
            m_Phase = Phase::AwaitFullscreenProfile;
        }
        else if (m_Phase == Phase::AwaitFullscreenQsfReady) {
            if (!writePhase(QStringLiteral("qsf-fullscreen-ready"))) {
                return;
            }
            // Re-authenticate a real data operation after the controlled
            // Moonlight fullscreen reconnect.  This prevents a ready signal
            // alone from being mistaken for a usable QSF session.
            m_Phase = Phase::AwaitFullscreenDownload;
            m_Qsf.downloadFile(m_Arguments.downloadName,
                               m_Arguments.fullscreenDownloadDestination);
        }
    }

    void onClipboardReceived()
    {
        if (m_Phase != Phase::AwaitGuestClipboard) {
            return;
        }
        if (m_Clipboard->text(QClipboard::Clipboard) != m_Arguments.expectedGuestClipboard) {
            // The first poll can legitimately race the guest coordinator and
            // return the client-first value it has not yet replaced. Keep
            // polling until the independently produced guest fixture arrives.
            return;
        }
        if (!writeAtomically(m_Arguments.receivedClipboardDestination,
                             m_Clipboard->text(QClipboard::Clipboard).toUtf8()) ||
            !writePhase(QStringLiteral("guest-clipboard-received"))) {
            fail(QStringLiteral("could not retain the Qt clipboard E2E result"));
            return;
        }
        m_Phase = Phase::AwaitDataOperations;
        m_Qsf.uploadFile(m_Arguments.uploadSource, m_Arguments.uploadName);
        m_Qsf.downloadFile(m_Arguments.downloadName, m_Arguments.downloadDestination);
    }

    void onClipboardSent()
    {
        if (m_Phase != Phase::AwaitInitialClientClipboardAck) {
            return;
        }
        if (!writePhase(QStringLiteral("client-clipboard-sent"))) {
            return;
        }
        m_Phase = Phase::AwaitGuestClipboard;
    }

    void onFileTransfer(const QString& description)
    {
        if (m_Phase == Phase::AwaitFullscreenDownload) {
            if (!description.startsWith(QStringLiteral("Downloaded ")) ||
                !filesEqual(m_Arguments.expectedDownloadSource,
                            m_Arguments.fullscreenDownloadDestination)) {
                fail(QStringLiteral("post-fullscreen QSF download differs from the guest fixture"));
                return;
            }
            if (!writePhase(QStringLiteral("fullscreen-download-received"))) {
                return;
            }
            m_Phase = Phase::Stopping;
            m_Moonlight.stopStream();
            return;
        }
        if (m_Phase != Phase::AwaitDataOperations) {
            return;
        }
        if (description.startsWith(QStringLiteral("Uploaded "))) {
            m_UploadFinished = true;
        }
        else if (description.startsWith(QStringLiteral("Downloaded "))) {
            if (!filesEqual(m_Arguments.expectedDownloadSource,
                            m_Arguments.downloadDestination)) {
                fail(QStringLiteral("QSF download bytes differ from the guest fixture"));
                return;
            }
            m_DownloadFinished = true;
        }
        else {
            fail(QStringLiteral("unknown QSF file completion result"));
            return;
        }
        completeDataOperationsIfReady();
    }

    void completeDataOperationsIfReady()
    {
        if (m_Phase != Phase::AwaitDataOperations || !m_UploadFinished ||
            !m_DownloadFinished) {
            return;
        }
        if (!writePhase(QStringLiteral("windowed-qsf-operations-complete"))) {
            return;
        }
        m_Phase = Phase::QuiescingNegotiated;
        if (!m_ProfileNegotiation.negotiate(
                QStringLiteral("%1x%2").arg(m_Arguments.resizeWidth)
                                          .arg(m_Arguments.resizeHeight),
                QStringLiteral("software"))) {
            fail(QStringLiteral("could not begin safe windowed profile negotiation: %1")
                     .arg(m_ProfileNegotiation.lastError()));
        }
    }

    void onConnectionProfileReceived(int width, int height, int fps, int bitrateKbps,
                                     const QString& videoCodec, bool qemuApplied)
    {
        if (m_Phase == Phase::AwaitNegotiatedProfile) {
            if (width != m_Arguments.resizeWidth || height != m_Arguments.resizeHeight ||
                fps != m_Arguments.expectedNegotiatedFps ||
                bitrateKbps != m_Arguments.expectedNegotiatedBitrateKbps ||
                videoCodec != m_Arguments.expectedNegotiatedVideoCodec || !qemuApplied) {
                fail(QStringLiteral("QSF did not return the expected windowed guest-confirmed connection profile"));
                return;
            }
            if (!writePhase(QStringLiteral("negotiated-profile-confirmed"))) {
                return;
            }
            // The production coordinator has already ended its profile-only
            // lease and queues the stopped-stream launch on the next event
            // turn. Set this phase before that launch can be observed.
            m_Phase = Phase::LaunchingNegotiated;
            return;
        }
        if (m_Phase == Phase::AwaitFullscreenProfile) {
            int expectedWidth = 0;
            int expectedHeight = 0;
            if (!parseResolution(m_Arguments.fullscreenResolution, &expectedWidth, &expectedHeight) ||
                width != expectedWidth || height != expectedHeight ||
                fps != m_Arguments.expectedFullscreenFps ||
                bitrateKbps != m_Arguments.expectedFullscreenBitrateKbps ||
                videoCodec != m_Arguments.expectedFullscreenVideoCodec || !qemuApplied) {
                fail(QStringLiteral("QSF did not return the expected fullscreen guest-confirmed connection profile"));
                return;
            }
            if (!writePhase(QStringLiteral("fullscreen-profile-confirmed"))) {
                return;
            }
            // The fullscreen presentation was saved before the old stream
            // stopped. The coordinator has independently received the guest
            // ACK and is now permitted to launch this presentation.
            m_Phase = Phase::LaunchingFullscreen;
        }
    }

    void fail(const QString& detail)
    {
        if (m_Phase == Phase::Failed || m_Phase == Phase::Complete) {
            return;
        }
        m_Phase = Phase::Failed;
        m_PhasePoll.stop();
        m_Timeout.stop();
        m_Qsf.setSessionActive(false);
        writeMoonlightLog();
        writePhase(QStringLiteral("failed"));
        const QString marker = m_Arguments.launchDescriptorMode
            ? QStringLiteral("QSUNSHINE_QT_DESCRIPTOR_E2E_FAILED=")
            : QStringLiteral("QSUNSHINE_QT_REAL_E2E_FAILED=");
        QTextStream(stderr) << marker << detail << '\n' << Qt::flush;
        QTimer::singleShot(0, &m_Application, [this]() { m_Application.exit(2); });
    }

    QGuiApplication& m_Application;
    Arguments m_Arguments;
    QsfClient m_Qsf;
    MoonlightController m_Moonlight;
    SystemAuthClient m_SystemAuth;
    ProfileNegotiationCoordinator m_ProfileNegotiation;
    QClipboard* m_Clipboard;
    QTimer m_PhasePoll;
    QTimer m_Timeout;
    Phase m_Phase = Phase::StartingWindowed;
    int m_StreamStarts = 0;
    bool m_UploadFinished = false;
    bool m_DownloadFinished = false;
};

} // namespace

int main(int argc, char* argv[])
{
    QStandardPaths::setTestModeEnabled(true);
    QGuiApplication::setOrganizationName(QStringLiteral("q-sunshine-tests"));
    QGuiApplication::setOrganizationDomain(QStringLiteral("q-sunshine.local"));
    QGuiApplication::setApplicationName(QStringLiteral("qsunshine-real-e2e"));
    QGuiApplication application(argc, argv);

    QCommandLineParser parser;
    parser.setApplicationDescription(
        QStringLiteral("Real Qt Moonlight/QSF E2E driver (legacy system-auth or PVE launch descriptor)"));
    parser.addHelpOption();
    parser.addOption({QStringLiteral("moonlight-binary"), QStringLiteral("patched Moonlight Qt executable"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("launch-file"),
                      QStringLiteral("one-use PVE .qsm launch authorization"),
                      QStringLiteral("path")});
    parser.addOption({QStringLiteral("descriptor-bootstrap-only"),
                      QStringLiteral("redeem --launch-file and start/stop Moonlight without activating QSF")});
    parser.addOption({QStringLiteral("host"), QStringLiteral("Sunshine host or host:base-port"), QStringLiteral("host")});
    parser.addOption({QStringLiteral("app"),
                      QStringLiteral("legacy Sunshine application (PVE descriptor mode always uses QEMU Console)"),
                      QStringLiteral("name"), QStringLiteral("QEMU Console")});
    parser.addOption({QStringLiteral("initial-resolution"), QStringLiteral("windowed Moonlight resolution"), QStringLiteral("resolution")});
    parser.addOption({QStringLiteral("fullscreen-resolution"), QStringLiteral("fullscreen reconnect resolution"), QStringLiteral("resolution")});
    parser.addOption({QStringLiteral("system-auth-host"), QStringLiteral("TLS/PAM system-auth gateway host"), QStringLiteral("host")});
    parser.addOption({QStringLiteral("system-auth-port"), QStringLiteral("TLS/PAM system-auth gateway port"), QStringLiteral("port")});
    parser.addOption({QStringLiteral("system-auth-server-name"), QStringLiteral("expected system-auth TLS server name"), QStringLiteral("name")});
    parser.addOption({QStringLiteral("system-auth-ca-file"), QStringLiteral("system-auth gateway CA PEM"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("system-auth-audience"), QStringLiteral("expected VM system-auth audience"), QStringLiteral("audience")});
    parser.addOption({QStringLiteral("system-auth-username"), QStringLiteral("system login username"), QStringLiteral("username")});
    parser.addOption({QStringLiteral("system-auth-password-stdin"), QStringLiteral("read one system login password line from stdin")});
    parser.addOption({QStringLiteral("qsf-host"), QStringLiteral("ticket-authenticated QSF gateway host"), QStringLiteral("host")});
    parser.addOption({QStringLiteral("qsf-port"), QStringLiteral("ticket-authenticated QSF gateway port"), QStringLiteral("port")});
    parser.addOption({QStringLiteral("qsf-server-name"), QStringLiteral("expected QSF TLS server name"), QStringLiteral("name")});
    parser.addOption({QStringLiteral("qsf-ca-file"), QStringLiteral("QSF gateway CA PEM"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("expected-guest-clipboard-file"), QStringLiteral("UTF-8 guest clipboard fixture"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("client-clipboard-file"), QStringLiteral("UTF-8 client clipboard fixture"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("upload-source"), QStringLiteral("local file uploaded through QSF"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("upload-name"), QStringLiteral("safe guest upload basename"), QStringLiteral("name")});
    parser.addOption({QStringLiteral("download-name"), QStringLiteral("safe guest outbox basename"), QStringLiteral("name")});
    parser.addOption({QStringLiteral("download-destination"), QStringLiteral("local QSF download path"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("fullscreen-download-destination"),
                      QStringLiteral("fresh local QSF download path after fullscreen reconnect"),
                      QStringLiteral("path")});
    parser.addOption({QStringLiteral("expected-download-source"), QStringLiteral("host fixture expected from the guest outbox"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("received-clipboard-destination"), QStringLiteral("retained Qt clipboard result path"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("resize"), QStringLiteral("client-selected QSF guest scanout WIDTHxHEIGHT"), QStringLiteral("resolution")});
    parser.addOption({QStringLiteral("expected-negotiated-fps"),
                      QStringLiteral("expected resolved stream FPS"), QStringLiteral("fps")});
    parser.addOption({QStringLiteral("expected-negotiated-bitrate-kbps"),
                      QStringLiteral("expected resolved stream bitrate in Kbps"),
                      QStringLiteral("kbps")});
    parser.addOption({QStringLiteral("expected-negotiated-video-codec"),
                      QStringLiteral("expected resolved public Moonlight codec"),
                      QStringLiteral("codec")});
    parser.addOption({QStringLiteral("expected-fullscreen-fps"),
                      QStringLiteral("expected resolved fullscreen stream FPS"),
                      QStringLiteral("fps")});
    parser.addOption({QStringLiteral("expected-fullscreen-bitrate-kbps"),
                      QStringLiteral("expected resolved fullscreen stream bitrate in Kbps"),
                      QStringLiteral("kbps")});
    parser.addOption({QStringLiteral("expected-fullscreen-video-codec"),
                      QStringLiteral("expected resolved fullscreen public Moonlight codec"),
                      QStringLiteral("codec")});
    parser.addOption({QStringLiteral("timeout-ms"),
                      QStringLiteral("whole real-E2E timeout in milliseconds"),
                      QStringLiteral("milliseconds"), QStringLiteral("300000")});
    parser.addOption({QStringLiteral("phase-dir"), QStringLiteral("fresh private harness phase directory"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("moonlight-log"), QStringLiteral("redacted Moonlight diagnostic output"), QStringLiteral("path")});
    parser.process(application);

    const bool launchDescriptorMode = parser.isSet(QStringLiteral("launch-file"));
    const bool descriptorBootstrapOnly = parser.isSet(QStringLiteral("descriptor-bootstrap-only"));
    if (descriptorBootstrapOnly && !launchDescriptorMode) {
        QTextStream(stderr) << "--descriptor-bootstrap-only requires --launch-file\n";
        return 2;
    }

    // The receiver mode is intentionally broker-authoritative. Reject every
    // former user-controlled ingress field before it can be copied into an
    // Arguments object, so this test driver cannot accidentally exercise a
    // direct PVE password or caller-provided VM route alongside a descriptor.
    const QStringList legacyIngressOptions {
        QStringLiteral("host"),
        QStringLiteral("app"),
        QStringLiteral("system-auth-host"),
        QStringLiteral("system-auth-port"),
        QStringLiteral("system-auth-server-name"),
        QStringLiteral("system-auth-ca-file"),
        QStringLiteral("system-auth-audience"),
        QStringLiteral("system-auth-username"),
        QStringLiteral("system-auth-password-stdin"),
        QStringLiteral("qsf-host"),
        QStringLiteral("qsf-port"),
        QStringLiteral("qsf-server-name"),
        QStringLiteral("qsf-ca-file"),
    };
    if (launchDescriptorMode) {
        for (const QString& option : legacyIngressOptions) {
            if (parser.isSet(option)) {
                QTextStream(stderr) << "--" << option
                                    << " is not accepted with --launch-file\n";
                return 2;
            }
        }
    }

    QStringList required {
        QStringLiteral("moonlight-binary"),
        QStringLiteral("initial-resolution"),
        QStringLiteral("phase-dir"),
    };
    if (launchDescriptorMode) {
        required.append(QStringLiteral("launch-file"));
        if (!descriptorBootstrapOnly) {
            required.append({
                QStringLiteral("fullscreen-resolution"),
                QStringLiteral("expected-guest-clipboard-file"),
                QStringLiteral("client-clipboard-file"),
                QStringLiteral("upload-source"), QStringLiteral("upload-name"),
                QStringLiteral("download-name"), QStringLiteral("download-destination"),
                QStringLiteral("fullscreen-download-destination"),
                QStringLiteral("expected-download-source"),
                QStringLiteral("received-clipboard-destination"),
                QStringLiteral("resize"), QStringLiteral("expected-negotiated-fps"),
                QStringLiteral("expected-negotiated-bitrate-kbps"),
                QStringLiteral("expected-negotiated-video-codec"),
                QStringLiteral("expected-fullscreen-fps"),
                QStringLiteral("expected-fullscreen-bitrate-kbps"),
                QStringLiteral("expected-fullscreen-video-codec"),
            });
        }
    }
    else {
        required.append({
            QStringLiteral("host"), QStringLiteral("fullscreen-resolution"),
            QStringLiteral("system-auth-host"), QStringLiteral("system-auth-port"),
            QStringLiteral("system-auth-ca-file"), QStringLiteral("system-auth-audience"),
            QStringLiteral("system-auth-username"), QStringLiteral("system-auth-password-stdin"),
            QStringLiteral("qsf-host"), QStringLiteral("qsf-port"),
            QStringLiteral("qsf-ca-file"),
            QStringLiteral("expected-guest-clipboard-file"), QStringLiteral("client-clipboard-file"),
            QStringLiteral("upload-source"), QStringLiteral("upload-name"),
            QStringLiteral("download-name"), QStringLiteral("download-destination"),
            QStringLiteral("fullscreen-download-destination"),
            QStringLiteral("expected-download-source"),
            QStringLiteral("received-clipboard-destination"),
            QStringLiteral("resize"), QStringLiteral("expected-negotiated-fps"),
            QStringLiteral("expected-negotiated-bitrate-kbps"),
            QStringLiteral("expected-negotiated-video-codec"),
            QStringLiteral("expected-fullscreen-fps"),
            QStringLiteral("expected-fullscreen-bitrate-kbps"),
            QStringLiteral("expected-fullscreen-video-codec"),
        });
    }
    for (const QString& option : required) {
        if (!parser.isSet(option)) {
            QTextStream(stderr) << "missing required option --" << option << '\n';
            return 2;
        }
    }

    Arguments arguments;
    arguments.launchDescriptorMode = launchDescriptorMode;
    arguments.descriptorBootstrapOnly = descriptorBootstrapOnly;
    arguments.launchFile = parser.value(QStringLiteral("launch-file")).trimmed();
    arguments.moonlightBinary = parser.value(QStringLiteral("moonlight-binary"));
    arguments.appName = parser.value(QStringLiteral("app"));
    arguments.initialResolution = parser.value(QStringLiteral("initial-resolution"));
    arguments.phaseDirectory = parser.value(QStringLiteral("phase-dir"));
    arguments.moonlightLog = parser.value(QStringLiteral("moonlight-log"));
    static const QRegularExpression timeoutPattern(QStringLiteral("\\A[0-9]{5,6}\\z"));
    int ignoredWidth = 0;
    int ignoredHeight = 0;
    bool timeoutOk = false;
    arguments.timeoutMs = parser.value(QStringLiteral("timeout-ms")).toInt(&timeoutOk);
    if (!timeoutPattern.match(parser.value(QStringLiteral("timeout-ms")).trimmed()).hasMatch() ||
        !timeoutOk || arguments.timeoutMs < 60000 || arguments.timeoutMs > 900000 ||
        !parseResolution(arguments.initialResolution, &ignoredWidth, &ignoredHeight)) {
        QTextStream(stderr) << "invalid timeout or initial-resolution argument\n";
        return 2;
    }

    if (!launchDescriptorMode) {
        static const QRegularExpression portPattern(QStringLiteral("\\A[0-9]{1,5}\\z"));
        bool qsfPortOk = false;
        bool systemAuthPortOk = false;
        bool expectedFpsOk = false;
        bool expectedBitrateOk = false;
        bool expectedFullscreenFpsOk = false;
        bool expectedFullscreenBitrateOk = false;
        arguments.host = parser.value(QStringLiteral("host"));
        arguments.fullscreenResolution = parser.value(QStringLiteral("fullscreen-resolution"));
        arguments.systemAuthHost = parser.value(QStringLiteral("system-auth-host"));
        arguments.systemAuthPort =
            parser.value(QStringLiteral("system-auth-port")).toInt(&systemAuthPortOk);
        arguments.systemAuthServerName = parser.value(QStringLiteral("system-auth-server-name"));
        arguments.systemAuthCaFile = parser.value(QStringLiteral("system-auth-ca-file"));
        arguments.systemAuthAudience = parser.value(QStringLiteral("system-auth-audience"));
        arguments.systemAuthUsername = parser.value(QStringLiteral("system-auth-username"));
        arguments.qsfHost = parser.value(QStringLiteral("qsf-host"));
        arguments.qsfPort = parser.value(QStringLiteral("qsf-port")).toInt(&qsfPortOk);
        arguments.qsfServerName = parser.value(QStringLiteral("qsf-server-name"));
        arguments.qsfCaFile = parser.value(QStringLiteral("qsf-ca-file"));
        arguments.expectedGuestClipboardFile =
            parser.value(QStringLiteral("expected-guest-clipboard-file"));
        arguments.clientClipboardFile = parser.value(QStringLiteral("client-clipboard-file"));
        arguments.uploadSource = parser.value(QStringLiteral("upload-source"));
        arguments.uploadName = parser.value(QStringLiteral("upload-name"));
        arguments.downloadName = parser.value(QStringLiteral("download-name"));
        arguments.downloadDestination = parser.value(QStringLiteral("download-destination"));
        arguments.fullscreenDownloadDestination =
            parser.value(QStringLiteral("fullscreen-download-destination"));
        arguments.expectedDownloadSource = parser.value(QStringLiteral("expected-download-source"));
        arguments.receivedClipboardDestination =
            parser.value(QStringLiteral("received-clipboard-destination"));
        arguments.expectedNegotiatedFps =
            parser.value(QStringLiteral("expected-negotiated-fps")).toInt(&expectedFpsOk);
        arguments.expectedNegotiatedBitrateKbps =
            parser.value(QStringLiteral("expected-negotiated-bitrate-kbps")).toInt(&expectedBitrateOk);
        arguments.expectedNegotiatedVideoCodec =
            parser.value(QStringLiteral("expected-negotiated-video-codec")).trimmed();
        arguments.expectedFullscreenFps =
            parser.value(QStringLiteral("expected-fullscreen-fps")).toInt(&expectedFullscreenFpsOk);
        arguments.expectedFullscreenBitrateKbps =
            parser.value(QStringLiteral("expected-fullscreen-bitrate-kbps"))
                .toInt(&expectedFullscreenBitrateOk);
        arguments.expectedFullscreenVideoCodec =
            parser.value(QStringLiteral("expected-fullscreen-video-codec")).trimmed();
        if (!portPattern.match(parser.value(QStringLiteral("qsf-port")).trimmed()).hasMatch() ||
            !portPattern.match(parser.value(QStringLiteral("system-auth-port")).trimmed()).hasMatch() ||
            !qsfPortOk || arguments.qsfPort < 1 || arguments.qsfPort > 65535 ||
            !systemAuthPortOk || arguments.systemAuthPort < 1 || arguments.systemAuthPort > 65535 ||
            !expectedFpsOk || arguments.expectedNegotiatedFps < 10 ||
            arguments.expectedNegotiatedFps > 240 || !expectedBitrateOk ||
            arguments.expectedNegotiatedBitrateKbps < 500 ||
            arguments.expectedNegotiatedBitrateKbps > 500000 ||
            (arguments.expectedNegotiatedVideoCodec != QStringLiteral("H.264") &&
             arguments.expectedNegotiatedVideoCodec != QStringLiteral("HEVC") &&
             arguments.expectedNegotiatedVideoCodec != QStringLiteral("AV1")) ||
            !expectedFullscreenFpsOk || arguments.expectedFullscreenFps < 10 ||
            arguments.expectedFullscreenFps > 240 || !expectedFullscreenBitrateOk ||
            arguments.expectedFullscreenBitrateKbps < 500 ||
            arguments.expectedFullscreenBitrateKbps > 500000 ||
            (arguments.expectedFullscreenVideoCodec != QStringLiteral("H.264") &&
             arguments.expectedFullscreenVideoCodec != QStringLiteral("HEVC") &&
             arguments.expectedFullscreenVideoCodec != QStringLiteral("AV1")) ||
            !parseResolution(arguments.fullscreenResolution, &ignoredWidth, &ignoredHeight) ||
            !parseResolution(parser.value(QStringLiteral("resize")), &arguments.resizeWidth,
                             &arguments.resizeHeight)) {
            QTextStream(stderr) << "invalid legacy system-auth/QSF endpoint, negotiated-profile, or resolution argument\n";
            return 2;
        }
        if (!readSystemAuthPassword(&arguments.systemAuthPassword)) {
            QTextStream(stderr) << "invalid or missing system-auth password on stdin\n";
            return 2;
        }
    }
    else if (!descriptorBootstrapOnly) {
        bool expectedFpsOk = false;
        bool expectedBitrateOk = false;
        bool expectedFullscreenFpsOk = false;
        bool expectedFullscreenBitrateOk = false;
        arguments.fullscreenResolution = parser.value(QStringLiteral("fullscreen-resolution"));
        arguments.expectedGuestClipboardFile =
            parser.value(QStringLiteral("expected-guest-clipboard-file"));
        arguments.clientClipboardFile = parser.value(QStringLiteral("client-clipboard-file"));
        arguments.uploadSource = parser.value(QStringLiteral("upload-source"));
        arguments.uploadName = parser.value(QStringLiteral("upload-name"));
        arguments.downloadName = parser.value(QStringLiteral("download-name"));
        arguments.downloadDestination = parser.value(QStringLiteral("download-destination"));
        arguments.fullscreenDownloadDestination =
            parser.value(QStringLiteral("fullscreen-download-destination"));
        arguments.expectedDownloadSource = parser.value(QStringLiteral("expected-download-source"));
        arguments.receivedClipboardDestination =
            parser.value(QStringLiteral("received-clipboard-destination"));
        arguments.expectedNegotiatedFps =
            parser.value(QStringLiteral("expected-negotiated-fps")).toInt(&expectedFpsOk);
        arguments.expectedNegotiatedBitrateKbps =
            parser.value(QStringLiteral("expected-negotiated-bitrate-kbps")).toInt(&expectedBitrateOk);
        arguments.expectedNegotiatedVideoCodec =
            parser.value(QStringLiteral("expected-negotiated-video-codec")).trimmed();
        arguments.expectedFullscreenFps =
            parser.value(QStringLiteral("expected-fullscreen-fps")).toInt(&expectedFullscreenFpsOk);
        arguments.expectedFullscreenBitrateKbps =
            parser.value(QStringLiteral("expected-fullscreen-bitrate-kbps"))
                .toInt(&expectedFullscreenBitrateOk);
        arguments.expectedFullscreenVideoCodec =
            parser.value(QStringLiteral("expected-fullscreen-video-codec")).trimmed();
        if (!expectedFpsOk || arguments.expectedNegotiatedFps < 10 ||
            arguments.expectedNegotiatedFps > 240 || !expectedBitrateOk ||
            arguments.expectedNegotiatedBitrateKbps < 500 ||
            arguments.expectedNegotiatedBitrateKbps > 500000 ||
            (arguments.expectedNegotiatedVideoCodec != QStringLiteral("H.264") &&
             arguments.expectedNegotiatedVideoCodec != QStringLiteral("HEVC") &&
             arguments.expectedNegotiatedVideoCodec != QStringLiteral("AV1")) ||
            !expectedFullscreenFpsOk || arguments.expectedFullscreenFps < 10 ||
            arguments.expectedFullscreenFps > 240 || !expectedFullscreenBitrateOk ||
            arguments.expectedFullscreenBitrateKbps < 500 ||
            arguments.expectedFullscreenBitrateKbps > 500000 ||
            (arguments.expectedFullscreenVideoCodec != QStringLiteral("H.264") &&
             arguments.expectedFullscreenVideoCodec != QStringLiteral("HEVC") &&
             arguments.expectedFullscreenVideoCodec != QStringLiteral("AV1")) ||
            !parseResolution(arguments.fullscreenResolution, &ignoredWidth, &ignoredHeight) ||
            !parseResolution(parser.value(QStringLiteral("resize")), &arguments.resizeWidth,
                             &arguments.resizeHeight)) {
            QTextStream(stderr) << "invalid descriptor E2E negotiated-profile or resolution argument\n";
            return 2;
        }
    }

    RealE2eDriver driver(application, std::move(arguments));
    QTimer::singleShot(0, &driver, [&driver]() { driver.start(); });
    return application.exec();
}
