// SPDX-License-Identifier: GPL-3.0-or-later
// Real-runtime orchestrator for the same Qt shell classes used by the GUI.
//
// The surrounding shell harness owns QEMU, Sunshine and the disposable client
// display. It creates activate-windowed only after it has verified the first
// Moonlight window, and activate-fullscreen only after it has verified the
// controlled reconnect. This preserves the product's explicit post-visible-
// video QSF activation boundary while making it automatable.

#include "moonlightcontroller.h"
#include "qsfclient.h"

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

#include <utility>

namespace {

struct Arguments {
    QString moonlightBinary;
    QString host;
    QString appName;
    QString pairPin;
    QString initialResolution;
    QString fullscreenResolution;
    QString qsfHost;
    int qsfPort = 0;
    QString qsfServerName;
    QString caFile;
    QString certificateFile;
    QString keyFile;
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
    int timeoutMs = 300000;
    QString phaseDirectory;
    QString moonlightLog;
    QString moonlightPairLog;
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

class RealE2eDriver final : public QObject
{
public:
    RealE2eDriver(QGuiApplication& application, Arguments arguments)
        : QObject(&application),
          m_Application(application),
          m_Arguments(std::move(arguments)),
          m_Qsf(this),
          m_Moonlight(this),
          m_Clipboard(QGuiApplication::clipboard())
    {
        m_PhasePoll.setInterval(100);
        connect(&m_PhasePoll, &QTimer::timeout, this, [this]() { pollHarnessPhases(); });

        m_Timeout.setSingleShot(true);
        m_Timeout.setInterval(m_Arguments.timeoutMs);
        connect(&m_Timeout, &QTimer::timeout, this, [this]() {
            fail(QStringLiteral("timed out waiting for real Qt/Moonlight/QSF E2E phases"));
        });

        connect(&m_Moonlight, &MoonlightController::streamStarted,
                this, [this]() { onStreamStarted(); });
        connect(&m_Moonlight, &MoonlightController::pairProcessStarted,
                this, [this]() { onPairProcessStarted(); });
        connect(&m_Moonlight, &MoonlightController::pairingChanged,
                this, [this]() { onPairingChanged(); });
        connect(&m_Moonlight, &MoonlightController::streamTeardownStarted,
                this, [this]() {
                    m_Qsf.setSessionActive(false);
                    const QString marker = m_Phase == Phase::ReconnectingFullscreen
                        ? QStringLiteral("qsf-deactivated-for-reconnect")
                        : (m_Phase == Phase::Stopping
                               ? QStringLiteral("qsf-deactivated-for-stop")
                               : QStringLiteral("qsf-deactivated"));
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
        connect(&m_Qsf, &QsfClient::resizeApplied,
                this, [this](int width, int height, bool qemuApplied) {
                    onResizeApplied(width, height, qemuApplied);
                });
        connect(&m_Qsf, &QsfClient::lastErrorChanged, this, [this]() {
            if (!m_Qsf.lastError().isEmpty()) {
                fail(QStringLiteral("QSF client: %1").arg(m_Qsf.lastError()));
            }
        });
        connect(&m_Application, &QGuiApplication::aboutToQuit, this, [this]() {
            m_Qsf.setSessionActive(false);
            m_Moonlight.cancelPairing();
            m_Moonlight.stopStream();
        });
    }

    void start()
    {
        if (m_Clipboard == nullptr) {
            fail(QStringLiteral("Qt has no clipboard implementation on this platform"));
            return;
        }
        const QDir phaseDirectory(m_Arguments.phaseDirectory);
        if (!QFileInfo(m_Arguments.phaseDirectory).isDir() ||
            !phaseDirectory.entryList(QDir::AllEntries | QDir::NoDotAndDotDot).isEmpty() ||
            !QFileInfo(m_Arguments.uploadSource).isFile() ||
            !QFileInfo(m_Arguments.expectedGuestClipboardFile).isFile() ||
            !QFileInfo(m_Arguments.clientClipboardFile).isFile() ||
            !QFileInfo(m_Arguments.expectedDownloadSource).isFile() ||
            !QFileInfo(m_Arguments.caFile).isFile() ||
            !QFileInfo(m_Arguments.certificateFile).isFile() ||
            !QFileInfo(m_Arguments.keyFile).isFile()) {
            fail(QStringLiteral("real E2E has a missing phase directory, fixture, or TLS credential"));
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
        if (!m_Arguments.moonlightLog.isEmpty() &&
            !QFileInfo(m_Arguments.moonlightLog).dir().exists()) {
            fail(QStringLiteral("Moonlight diagnostic log parent does not exist"));
            return;
        }
        if (!m_Arguments.moonlightPairLog.isEmpty() &&
            !QFileInfo(m_Arguments.moonlightPairLog).dir().exists()) {
            fail(QStringLiteral("Moonlight pairing diagnostic log parent does not exist"));
            return;
        }
        for (const QString& marker : {QStringLiteral("activate-windowed"),
                                      QStringLiteral("activate-fullscreen")}) {
            if (phaseRequested(marker)) {
                fail(QStringLiteral("stale harness marker exists: %1").arg(marker));
                return;
            }
        }

        m_Moonlight.setBinaryPath(m_Arguments.moonlightBinary);
        // Xvfb cannot read back an NVIDIA VDPAU presentation surface with
        // xwd. Force the production controller's constrained software choice
        // only in this disposable visual-attestation driver, so the non-black
        // screenshot proves decoded pixels rather than an overlay placeholder.
        m_Moonlight.setVideoDecoder(QStringLiteral("software"));
        if (m_Moonlight.videoDecoder() != QStringLiteral("software")) {
            fail(QStringLiteral("could not select the Moonlight software decoder for visual attestation"));
            return;
        }
        if (!m_Moonlight.saveProfile(QStringLiteral("real-qt-e2e"), m_Arguments.host,
                                     m_Arguments.appName, m_Arguments.initialResolution,
                                     QStringLiteral("windowed"))) {
            fail(QStringLiteral("could not save the real E2E desktop profile: %1")
                     .arg(m_Moonlight.lastError()));
            return;
        }
        m_Qsf.selectProfile(m_Moonlight.currentProfileId());
        if (!m_Qsf.applyConfiguration(m_Arguments.qsfHost, m_Arguments.qsfPort,
                                      m_Arguments.qsfServerName, m_Arguments.caFile,
                                      m_Arguments.certificateFile, m_Arguments.keyFile)) {
            fail(QStringLiteral("could not configure QSF: %1").arg(m_Qsf.lastError()));
            return;
        }

        // The guest's native-Wayland fixture intentionally waits for this
        // client value before it creates its independent guest-side copy. This
        // exercises both directions in a causally unambiguous order.
        m_Qsf.setInitialClipboardDirection(QStringLiteral("client"));
        m_Qsf.setClipboardSyncEnabled(true);
        m_Clipboard->setText(m_Arguments.clientClipboard, QClipboard::Clipboard);

        if (!writePhase(QStringLiteral("driver-ready"))) {
            return;
        }
        m_Phase = Phase::Pairing;
        m_Timeout.start();
        m_PhasePoll.start();
        // Exercise the production pairing controller too. The isolated shell
        // harness supplies the matching ephemeral PIN to its headless
        // Sunshine stdin only after the real getservercert request, then
        // proves pairing independently with `moonlight list` before it writes
        // the pair-verified marker accepted below.
        m_Moonlight.pair(m_Arguments.host, m_Arguments.pairPin);
    }

private:
    enum class Phase {
        Pairing,
        AwaitPairVerification,
        StartingWindowed,
        AwaitWindowedVerification,
        AwaitWindowedQsfReady,
        AwaitInitialClientClipboardAck,
        AwaitGuestClipboard,
        AwaitDataOperations,
        AwaitFullscreenReconnectGate,
        ReconnectingFullscreen,
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

    bool writeMoonlightPairLog()
    {
        if (!m_Arguments.moonlightPairLog.isEmpty() &&
            !writeAtomically(m_Arguments.moonlightPairLog,
                             m_Moonlight.recentOutput().toUtf8())) {
            fail(QStringLiteral("could not retain redacted Moonlight pairing diagnostics"));
            return false;
        }
        return true;
    }

    void startWindowedStream()
    {
        m_Phase = Phase::StartingWindowed;
        m_Moonlight.startStream(m_Arguments.host, m_Arguments.appName,
                                m_Arguments.initialResolution,
                                QStringLiteral("windowed"));
    }

    void pollHarnessPhases()
    {
        if (m_Phase == Phase::AwaitPairVerification &&
            phaseRequested(QStringLiteral("pair-verified"))) {
            if (!writePhase(QStringLiteral("pair-verification-accepted"))) {
                return;
            }
            startWindowedStream();
        }
        else if (m_Phase == Phase::AwaitWindowedVerification &&
            phaseRequested(QStringLiteral("activate-windowed"))) {
            if (!writePhase(QStringLiteral("windowed-activation-accepted"))) {
                return;
            }
            m_Phase = Phase::AwaitWindowedQsfReady;
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
        else if (m_Phase == Phase::AwaitFullscreenReconnectGate &&
                 phaseRequested(QStringLiteral("begin-fullscreen-reconnect"))) {
            if (!writePhase(QStringLiteral("fullscreen-reconnect-accepted"))) {
                return;
            }
            // QSF resize acknowledgement only proves SetUIInfo reached QEMU.
            // The outer guest fixture writes this request after its Weston DRM
            // restart is independently complete, so reconnect never races a
            // disappearing pre-resize scanout.
            m_Phase = Phase::ReconnectingFullscreen;
            m_Moonlight.startStream(m_Arguments.host, m_Arguments.appName,
                                    m_Arguments.fullscreenResolution,
                                    QStringLiteral("fullscreen"));
        }
    }

    void onPairProcessStarted()
    {
        if (m_Phase != Phase::Pairing) {
            return;
        }
        m_PairProcessStarted = true;
        if (!writePhase(QStringLiteral("pair-process-started"))) {
            return;
        }
    }

    void onPairingChanged()
    {
        if (m_Moonlight.pairing() || m_Phase != Phase::Pairing ||
            !m_PairProcessStarted) {
            return;
        }
        if (!writeMoonlightPairLog() ||
            !writePhase(QStringLiteral("pair-process-finished"))) {
            return;
        }
        m_Phase = Phase::AwaitPairVerification;
    }

    void onStreamStarted()
    {
        ++m_StreamStarts;
        if (m_StreamStarts == 1 && m_Phase == Phase::StartingWindowed) {
            if (!writePhase(QStringLiteral("windowed-process-started"))) {
                return;
            }
            m_Phase = Phase::AwaitWindowedVerification;
            return;
        }
        if (m_StreamStarts == 2 && m_Phase == Phase::ReconnectingFullscreen) {
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
        if (m_Phase == Phase::ReconnectingFullscreen ||
            m_Phase == Phase::AwaitFullscreenVerification ||
            m_Phase == Phase::AwaitFullscreenQsfReady) {
            // This is the first child exiting during controlled reconnect.
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
            QTextStream(stdout) << "QSUNSHINE_QT_REAL_E2E_OK\n" << Qt::flush;
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
        m_Qsf.requestResize(m_Arguments.resizeWidth, m_Arguments.resizeHeight);
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

    void onResizeApplied(int width, int height, bool qemuApplied)
    {
        if (m_Phase != Phase::AwaitDataOperations) {
            return;
        }
        if (width != m_Arguments.resizeWidth || height != m_Arguments.resizeHeight ||
            !qemuApplied) {
            fail(QStringLiteral("real QSF resize was not accepted by QEMU SetUIInfo"));
            return;
        }
        m_ResizeFinished = true;
        completeDataOperationsIfReady();
    }

    void completeDataOperationsIfReady()
    {
        if (m_Phase != Phase::AwaitDataOperations || !m_UploadFinished ||
            !m_DownloadFinished || !m_ResizeFinished) {
            return;
        }
        if (!writePhase(QStringLiteral("windowed-qsf-operations-complete"))) {
            return;
        }
        m_Phase = Phase::AwaitFullscreenReconnectGate;
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
        QTextStream(stderr) << "QSUNSHINE_QT_REAL_E2E_FAILED=" << detail << '\n' << Qt::flush;
        QTimer::singleShot(0, &m_Application, [this]() { m_Application.exit(2); });
    }

    QGuiApplication& m_Application;
    Arguments m_Arguments;
    QsfClient m_Qsf;
    MoonlightController m_Moonlight;
    QClipboard* m_Clipboard;
    QTimer m_PhasePoll;
    QTimer m_Timeout;
    Phase m_Phase = Phase::StartingWindowed;
    int m_StreamStarts = 0;
    bool m_UploadFinished = false;
    bool m_DownloadFinished = false;
    bool m_ResizeFinished = false;
    bool m_PairProcessStarted = false;
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
    parser.setApplicationDescription(QStringLiteral("Real Qt shell/Moonlight/QSF E2E driver"));
    parser.addHelpOption();
    parser.addOption({QStringLiteral("moonlight-binary"), QStringLiteral("stock Moonlight Qt executable"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("host"), QStringLiteral("Sunshine host or host:base-port"), QStringLiteral("host")});
    parser.addOption({QStringLiteral("app"), QStringLiteral("Sunshine application"), QStringLiteral("name"), QStringLiteral("Desktop")});
    parser.addOption({QStringLiteral("pair-pin"), QStringLiteral("fresh four-digit Sunshine pairing PIN"), QStringLiteral("pin")});
    parser.addOption({QStringLiteral("initial-resolution"), QStringLiteral("windowed Moonlight resolution"), QStringLiteral("resolution")});
    parser.addOption({QStringLiteral("fullscreen-resolution"), QStringLiteral("fullscreen reconnect resolution"), QStringLiteral("resolution")});
    parser.addOption({QStringLiteral("qsf-host"), QStringLiteral("QSF mTLS gateway host"), QStringLiteral("host")});
    parser.addOption({QStringLiteral("qsf-port"), QStringLiteral("QSF mTLS gateway port"), QStringLiteral("port")});
    parser.addOption({QStringLiteral("qsf-server-name"), QStringLiteral("expected QSF TLS server name"), QStringLiteral("name")});
    parser.addOption({QStringLiteral("ca-file"), QStringLiteral("QSF gateway CA PEM"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("cert-file"), QStringLiteral("QSF client certificate PEM"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("key-file"), QStringLiteral("QSF client key PEM"), QStringLiteral("path")});
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
    parser.addOption({QStringLiteral("resize"), QStringLiteral("QSF guest resize WIDTHxHEIGHT"), QStringLiteral("resolution")});
    parser.addOption({QStringLiteral("timeout-ms"),
                      QStringLiteral("whole real-E2E timeout in milliseconds"),
                      QStringLiteral("milliseconds"), QStringLiteral("300000")});
    parser.addOption({QStringLiteral("phase-dir"), QStringLiteral("fresh private harness phase directory"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("moonlight-log"), QStringLiteral("redacted Moonlight diagnostic output"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("moonlight-pair-log"), QStringLiteral("redacted Moonlight pairing diagnostic output"), QStringLiteral("path")});
    parser.process(application);

    const QStringList required = {
        QStringLiteral("moonlight-binary"), QStringLiteral("host"), QStringLiteral("pair-pin"),
        QStringLiteral("initial-resolution"), QStringLiteral("fullscreen-resolution"),
        QStringLiteral("qsf-host"), QStringLiteral("qsf-port"),
        QStringLiteral("ca-file"), QStringLiteral("cert-file"), QStringLiteral("key-file"),
        QStringLiteral("expected-guest-clipboard-file"), QStringLiteral("client-clipboard-file"),
        QStringLiteral("upload-source"), QStringLiteral("upload-name"),
        QStringLiteral("download-name"), QStringLiteral("download-destination"),
        QStringLiteral("fullscreen-download-destination"),
        QStringLiteral("expected-download-source"),
        QStringLiteral("received-clipboard-destination"),
        QStringLiteral("resize"), QStringLiteral("phase-dir"),
    };
    for (const QString& option : required) {
        if (!parser.isSet(option)) {
            QTextStream(stderr) << "missing required option --" << option << '\n';
            return 2;
        }
    }

    bool portOk = false;
    Arguments arguments;
    arguments.moonlightBinary = parser.value(QStringLiteral("moonlight-binary"));
    arguments.host = parser.value(QStringLiteral("host"));
    arguments.appName = parser.value(QStringLiteral("app"));
    arguments.pairPin = parser.value(QStringLiteral("pair-pin"));
    arguments.initialResolution = parser.value(QStringLiteral("initial-resolution"));
    arguments.fullscreenResolution = parser.value(QStringLiteral("fullscreen-resolution"));
    arguments.qsfHost = parser.value(QStringLiteral("qsf-host"));
    arguments.qsfPort = parser.value(QStringLiteral("qsf-port")).toInt(&portOk);
    arguments.qsfServerName = parser.value(QStringLiteral("qsf-server-name"));
    arguments.caFile = parser.value(QStringLiteral("ca-file"));
    arguments.certificateFile = parser.value(QStringLiteral("cert-file"));
    arguments.keyFile = parser.value(QStringLiteral("key-file"));
    arguments.expectedGuestClipboardFile = parser.value(QStringLiteral("expected-guest-clipboard-file"));
    arguments.clientClipboardFile = parser.value(QStringLiteral("client-clipboard-file"));
    arguments.uploadSource = parser.value(QStringLiteral("upload-source"));
    arguments.uploadName = parser.value(QStringLiteral("upload-name"));
    arguments.downloadName = parser.value(QStringLiteral("download-name"));
    arguments.downloadDestination = parser.value(QStringLiteral("download-destination"));
    arguments.fullscreenDownloadDestination =
        parser.value(QStringLiteral("fullscreen-download-destination"));
    arguments.expectedDownloadSource = parser.value(QStringLiteral("expected-download-source"));
    arguments.receivedClipboardDestination = parser.value(QStringLiteral("received-clipboard-destination"));
    arguments.phaseDirectory = parser.value(QStringLiteral("phase-dir"));
    arguments.moonlightLog = parser.value(QStringLiteral("moonlight-log"));
    arguments.moonlightPairLog = parser.value(QStringLiteral("moonlight-pair-log"));
    static const QRegularExpression portPattern(QStringLiteral("\\A[0-9]{1,5}\\z"));
    static const QRegularExpression timeoutPattern(QStringLiteral("\\A[0-9]{5,6}\\z"));
    int ignoredWidth = 0;
    int ignoredHeight = 0;
    static const QRegularExpression pinPattern(QStringLiteral("\\A[0-9]{4}\\z"));
    bool timeoutOk = false;
    arguments.timeoutMs = parser.value(QStringLiteral("timeout-ms")).toInt(&timeoutOk);
    if (!portPattern.match(parser.value(QStringLiteral("qsf-port")).trimmed()).hasMatch() ||
        !pinPattern.match(arguments.pairPin).hasMatch() ||
        !timeoutPattern.match(parser.value(QStringLiteral("timeout-ms")).trimmed()).hasMatch() ||
        !portOk || arguments.qsfPort < 1 || arguments.qsfPort > 65535 ||
        !timeoutOk || arguments.timeoutMs < 60000 || arguments.timeoutMs > 900000 ||
        !parseResolution(arguments.initialResolution, &ignoredWidth, &ignoredHeight) ||
        !parseResolution(arguments.fullscreenResolution, &ignoredWidth, &ignoredHeight) ||
        !parseResolution(parser.value(QStringLiteral("resize")), &arguments.resizeWidth,
                         &arguments.resizeHeight)) {
        QTextStream(stderr) << "invalid port, timeout, or resolution argument\n";
        return 2;
    }

    RealE2eDriver driver(application, std::move(arguments));
    QTimer::singleShot(0, &driver, [&driver]() { driver.start(); });
    return application.exec();
}
