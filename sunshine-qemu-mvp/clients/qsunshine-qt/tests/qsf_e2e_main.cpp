// SPDX-License-Identifier: GPL-3.0-or-later
// Headless exercise of the same QsfClient used by the desktop shell.

#include "qsfclient.h"

#include <QClipboard>
#include <QCommandLineParser>
#include <QFileInfo>
#include <QGuiApplication>
#include <QRegularExpression>
#include <QStandardPaths>
#include <QTextStream>
#include <QTimer>

// The production API keeps connection_optimize coordinator-only so QML cannot
// submit a scanout transaction beneath a visible stream. This fixture needs to
// exercise QsfClient's wire validation in isolation, so it is the one narrow
// test friend declared by qsfclient.h.
class QsfClientTestAccess
{
public:
    static void optimizeConnectionForDisplay(QsfClient& client,
                                             const QString& requestedResolution,
                                             const QString& decoderPreference)
    {
        client.optimizeConnectionForDisplay(requestedResolution, decoderPreference);
    }
};

namespace {

struct Arguments {
    QString host;
    quint16 port = 0;
    QString serverName;
    QString caFile;
    QString certificateFile;
    QString keyFile;
    QString expectedGuestClipboard;
    QString clientClipboard;
    QString uploadSource;
    QString uploadName;
    QString downloadName;
    QString downloadDestination;
    int optimizedWidth = 0;
    int optimizedHeight = 0;
    int optimizedFps = 0;
    int optimizedBitrateKbps = 0;
    QString optimizedCodec;
    int resizeWidth = 0;
    int resizeHeight = 0;
};

bool parseResolution(const QString& value, int* width, int* height)
{
    static const QRegularExpression pattern(QStringLiteral("\\A([0-9]{2,5})x([0-9]{2,5})\\z"));
    const QRegularExpressionMatch match = pattern.match(value);
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

class QsfE2eDriver final : public QObject
{
public:
    QsfE2eDriver(QGuiApplication& application, const Arguments& arguments)
        : QObject(&application),
          m_Application(application),
          m_Arguments(arguments),
          m_Client(this),
          m_Clipboard(QGuiApplication::clipboard())
    {
        m_Timeout.setSingleShot(true);
        // The tested client permits one full 85-second profile request; the
        // whole driver must not reject a valid delayed guest ACK first.
        m_Timeout.setInterval(100000);
        connect(&m_Timeout, &QTimer::timeout, this, [this]() {
            fail(QStringLiteral("timed out waiting for QSF desktop-client operations"));
        });
        connect(&m_Client, &QsfClient::readyChanged, this, [this]() {
            if (m_Client.ready() && m_Phase == Phase::WaitingForGateway) {
                m_Phase = Phase::WaitingForOptimization;
                QsfClientTestAccess::optimizeConnectionForDisplay(m_Client,
                    QStringLiteral("%1x%2").arg(m_Arguments.optimizedWidth)
                                             .arg(m_Arguments.optimizedHeight),
                    QStringLiteral("auto"));
            }
        });
        connect(&m_Client, &QsfClient::connectionProfileReceived, this,
                [this](int width, int height, int fps, int bitrateKbps,
                       const QString& videoCodec, bool qemuApplied) {
                    if (m_Phase != Phase::WaitingForOptimization ||
                        width != m_Arguments.optimizedWidth ||
                        height != m_Arguments.optimizedHeight ||
                        fps != m_Arguments.optimizedFps ||
                        bitrateKbps != m_Arguments.optimizedBitrateKbps ||
                        videoCodec != m_Arguments.optimizedCodec || qemuApplied) {
                        fail(QStringLiteral("QSF host capability selection returned an unexpected profile"));
                        return;
                    }
                    m_OptimizationFinished = true;
                    m_Phase = Phase::WaitingForGuestClipboard;
                    // Keep guest-to-client ordering deterministic: activate
                    // clipboard synchronization only after the guest has
                    // requested and selected the host capability envelope.
                    m_Client.setClipboardSyncEnabled(true);
                });
        connect(&m_Client, &QsfClient::clipboardReceivedFromGuest, this, [this]() {
            onClipboardReceived();
        });
        connect(&m_Client, &QsfClient::clipboardSentToGuest, this, [this]() {
            onClipboardSent();
        });
        connect(&m_Client, &QsfClient::fileTransferFinished, this,
                [this](const QString& description) { onFileTransfer(description); });
        connect(&m_Client, &QsfClient::resizeApplied, this,
                [this](int width, int height, bool) {
                    if (width != m_Arguments.resizeWidth || height != m_Arguments.resizeHeight) {
                        fail(QStringLiteral("QSF acknowledged an unexpected resize"));
                        return;
                    }
                    m_ResizeFinished = true;
                    completeIfReady();
                });
        connect(&m_Client, &QsfClient::lastErrorChanged, this, [this]() {
            if (!m_Client.lastError().isEmpty()) {
                fail(m_Client.lastError());
            }
        });
    }

    void start()
    {
        if (m_Clipboard == nullptr) {
            fail(QStringLiteral("Qt has no clipboard implementation on this platform"));
            return;
        }
        if (!QFileInfo::exists(m_Arguments.uploadSource)) {
            fail(QStringLiteral("the QSF upload fixture does not exist"));
            return;
        }

        m_Client.applyConfiguration(m_Arguments.host, m_Arguments.port, m_Arguments.serverName,
                                    m_Arguments.caFile, m_Arguments.certificateFile,
                                    m_Arguments.keyFile);
        if (!m_Client.configured()) {
            fail(QStringLiteral("the QSF test endpoint was rejected before activation"));
            return;
        }
        // This mirrors the desktop shell's explicit post-video activation
        // handoff. The test fixture has no Moonlight process, so it supplies
        // that trusted handoff directly.
        // This test intentionally begins with guest-to-client propagation;
        // production defaults to client-first and exposes the policy in UI.
        m_Client.setInitialClipboardDirection(QStringLiteral("guest"));
        m_Client.setClipboardSyncEnabled(false);
        m_Phase = Phase::WaitingForGateway;
        m_Timeout.start();
        m_Client.setSessionActive(true);
    }

private:
    enum class Phase {
        WaitingForGateway,
        WaitingForOptimization,
        WaitingForGuestClipboard,
        WaitingForClientClipboard,
        WaitingForOperations,
        Complete,
        Failed,
    };

    void onClipboardReceived()
    {
        if (m_Phase != Phase::WaitingForGuestClipboard) {
            return;
        }
        if (m_Clipboard->text(QClipboard::Clipboard) != m_Arguments.expectedGuestClipboard) {
            fail(QStringLiteral("guest-to-client clipboard text did not reach Qt"));
            return;
        }
        m_Phase = Phase::WaitingForClientClipboard;
        m_Clipboard->setText(m_Arguments.clientClipboard, QClipboard::Clipboard);
    }

    void onClipboardSent()
    {
        if (m_Phase != Phase::WaitingForClientClipboard) {
            return;
        }
        m_Phase = Phase::WaitingForOperations;
        m_Client.uploadFile(m_Arguments.uploadSource, m_Arguments.uploadName);
        m_Client.downloadFile(m_Arguments.downloadName, m_Arguments.downloadDestination);
        m_Client.requestResize(m_Arguments.resizeWidth, m_Arguments.resizeHeight);
    }

    void onFileTransfer(const QString& description)
    {
        if (m_Phase != Phase::WaitingForOperations) {
            return;
        }
        if (description.startsWith(QStringLiteral("Uploaded "))) {
            m_UploadFinished = true;
        }
        else if (description.startsWith(QStringLiteral("Downloaded "))) {
            m_DownloadFinished = true;
        }
        else {
            fail(QStringLiteral("QSF reported an unknown file-transfer completion"));
            return;
        }
        completeIfReady();
    }

    void completeIfReady()
    {
        if (m_Phase != Phase::WaitingForOperations || !m_OptimizationFinished ||
            !m_UploadFinished || !m_DownloadFinished || !m_ResizeFinished) {
            return;
        }
        m_Phase = Phase::Complete;
        m_Timeout.stop();
        m_Client.setSessionActive(false);
        QTextStream(stdout) << "QSF_QT_CLIENT_HOST_OPTIMIZATION_OK\n"
                            << "QSF_QT_CLIENT_E2E_OK\n" << Qt::flush;
        QTimer::singleShot(0, &m_Application, [this]() {
            m_Application.exit(0);
        });
    }

    void fail(const QString& detail)
    {
        if (m_Phase == Phase::Complete || m_Phase == Phase::Failed) {
            return;
        }
        m_Phase = Phase::Failed;
        m_Timeout.stop();
        m_Client.setSessionActive(false);
        QTextStream(stderr) << "QSF_QT_CLIENT_E2E_FAILED=" << detail << '\n' << Qt::flush;
        QTimer::singleShot(0, &m_Application, [this]() {
            m_Application.exit(2);
        });
    }

    QGuiApplication& m_Application;
    Arguments m_Arguments;
    QsfClient m_Client;
    QClipboard* m_Clipboard;
    QTimer m_Timeout;
    Phase m_Phase = Phase::WaitingForGateway;
    bool m_UploadFinished = false;
    bool m_DownloadFinished = false;
    bool m_ResizeFinished = false;
    bool m_OptimizationFinished = false;
};

} // namespace

int main(int argc, char* argv[])
{
    QStandardPaths::setTestModeEnabled(true);
    QGuiApplication::setOrganizationName(QStringLiteral("q-sunshine-tests"));
    QGuiApplication::setOrganizationDomain(QStringLiteral("q-sunshine.local"));
    QGuiApplication::setApplicationName(QStringLiteral("qsunshine-qsf-e2e"));
    QGuiApplication application(argc, argv);

    QCommandLineParser parser;
    parser.setApplicationDescription(QStringLiteral("Headless QSF Qt-client mTLS E2E driver"));
    parser.addHelpOption();
    parser.addOption({QStringLiteral("host"), QStringLiteral("QSF gateway host"), QStringLiteral("host")});
    parser.addOption({QStringLiteral("port"), QStringLiteral("QSF gateway port"), QStringLiteral("port")});
    parser.addOption({QStringLiteral("server-name"), QStringLiteral("expected TLS server name"), QStringLiteral("name")});
    parser.addOption({QStringLiteral("ca-file"), QStringLiteral("gateway CA PEM"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("cert-file"), QStringLiteral("client certificate PEM"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("key-file"), QStringLiteral("client private key PEM"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("expected-guest-clipboard"), QStringLiteral("expected initial guest clipboard"), QStringLiteral("text")});
    parser.addOption({QStringLiteral("client-clipboard"), QStringLiteral("clipboard text to send to guest"), QStringLiteral("text")});
    parser.addOption({QStringLiteral("upload-source"), QStringLiteral("local upload fixture"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("upload-name"), QStringLiteral("guest upload basename"), QStringLiteral("name")});
    parser.addOption({QStringLiteral("download-name"), QStringLiteral("guest download basename"), QStringLiteral("name")});
    parser.addOption({QStringLiteral("download-destination"), QStringLiteral("local download destination"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("optimized-resolution"), QStringLiteral("expected guest-selected WIDTHxHEIGHT profile"), QStringLiteral("resolution")});
    parser.addOption({QStringLiteral("optimized-fps"), QStringLiteral("expected guest-selected FPS"), QStringLiteral("fps")});
    parser.addOption({QStringLiteral("optimized-bitrate"), QStringLiteral("expected guest-selected bitrate in Kbps"), QStringLiteral("kbps")});
    parser.addOption({QStringLiteral("optimized-codec"), QStringLiteral("expected guest-selected codec"), QStringLiteral("codec")});
    parser.addOption({QStringLiteral("resize"), QStringLiteral("guest resize WIDTHxHEIGHT"), QStringLiteral("resolution")});
    parser.process(application);

    const QStringList required = {
        QStringLiteral("host"), QStringLiteral("port"), QStringLiteral("ca-file"),
        QStringLiteral("cert-file"), QStringLiteral("key-file"),
        QStringLiteral("expected-guest-clipboard"), QStringLiteral("client-clipboard"),
        QStringLiteral("upload-source"), QStringLiteral("upload-name"),
        QStringLiteral("download-name"), QStringLiteral("download-destination"),
        QStringLiteral("optimized-resolution"), QStringLiteral("optimized-fps"),
        QStringLiteral("optimized-bitrate"), QStringLiteral("optimized-codec"),
        QStringLiteral("resize"),
    };
    for (const QString& option : required) {
        if (!parser.isSet(option)) {
            QTextStream(stderr) << "missing required option --" << option << '\n';
            return 2;
        }
    }

    bool portOk = false;
    const int port = parser.value(QStringLiteral("port")).toInt(&portOk);
    Arguments arguments;
    arguments.host = parser.value(QStringLiteral("host"));
    arguments.port = static_cast<quint16>(port);
    arguments.serverName = parser.value(QStringLiteral("server-name"));
    arguments.caFile = parser.value(QStringLiteral("ca-file"));
    arguments.certificateFile = parser.value(QStringLiteral("cert-file"));
    arguments.keyFile = parser.value(QStringLiteral("key-file"));
    arguments.expectedGuestClipboard = parser.value(QStringLiteral("expected-guest-clipboard"));
    arguments.clientClipboard = parser.value(QStringLiteral("client-clipboard"));
    arguments.uploadSource = parser.value(QStringLiteral("upload-source"));
    arguments.uploadName = parser.value(QStringLiteral("upload-name"));
    arguments.downloadName = parser.value(QStringLiteral("download-name"));
    arguments.downloadDestination = parser.value(QStringLiteral("download-destination"));
    bool optimizedFpsOk = false;
    bool optimizedBitrateOk = false;
    arguments.optimizedFps = parser.value(QStringLiteral("optimized-fps")).toInt(&optimizedFpsOk);
    arguments.optimizedBitrateKbps = parser.value(QStringLiteral("optimized-bitrate")).toInt(&optimizedBitrateOk);
    arguments.optimizedCodec = parser.value(QStringLiteral("optimized-codec"));
    if (!portOk || port < 1 || port > 65535 || !optimizedFpsOk ||
        !optimizedBitrateOk || arguments.optimizedFps < 10 || arguments.optimizedFps > 240 ||
        arguments.optimizedBitrateKbps < 500 || arguments.optimizedBitrateKbps > 500000 ||
        (arguments.optimizedCodec != QStringLiteral("H.264") &&
         arguments.optimizedCodec != QStringLiteral("HEVC") &&
         arguments.optimizedCodec != QStringLiteral("AV1")) ||
        !parseResolution(parser.value(QStringLiteral("optimized-resolution")),
                         &arguments.optimizedWidth, &arguments.optimizedHeight) ||
        !parseResolution(parser.value(QStringLiteral("resize")), &arguments.resizeWidth,
                         &arguments.resizeHeight)) {
        QTextStream(stderr) << "invalid QSF connection-profile or resize argument\n";
        return 2;
    }

    QsfE2eDriver driver(application, arguments);
    QTimer::singleShot(0, &driver, [&driver]() { driver.start(); });
    return application.exec();
}
