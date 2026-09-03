// SPDX-License-Identifier: GPL-3.0-or-later
// Drives the native receiver against a disposable TLS broker fixture.

#include "qsfclient.h"
#include "systemauthclient.h"

#include <QCommandLineParser>
#include <QDateTime>
#include <QFile>
#include <QFileDevice>
#include <QFileInfo>
#include <QGuiApplication>
#include <QSettings>
#include <QStandardPaths>
#include <QTemporaryFile>
#include <QTextStream>
#include <QTimer>

class SystemAuthClientTestAccess
{
public:
    static QString brokerCaPath(const SystemAuthClient& client)
    {
        return client.m_BrokerQsfCaFile ? client.m_BrokerQsfCaFile->fileName() : QString();
    }

    static bool launchStateCleared(const SystemAuthClient& client)
    {
        return !client.m_LaunchDescriptorMode && client.m_LaunchCaPem.isEmpty() &&
               client.m_SecretRequest.isEmpty();
    }
};

namespace {

const QByteArray kExpectedTicket = QByteArrayLiteral(
    "qsa1.eyJhdWQiOiJ2bS0xMDAifQ.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA");

bool require(bool condition, const QString& message)
{
    if (!condition) {
        QTextStream(stderr) << "QSUNSHINE_LAUNCH_DESCRIPTOR_TLS_TEST_FAILED=" << message << '\n';
    }
    return condition;
}

bool settingsContain(const QString& value)
{
    QSettings settings;
    for (const QString& key : settings.allKeys()) {
        if (settings.value(key).toString().contains(value)) {
            return true;
        }
    }
    return false;
}

bool isOwnerPrivateFile(const QString& path)
{
    const QFileInfo info(path);
    const QFileDevice::Permissions unsafe = QFileDevice::ReadGroup | QFileDevice::WriteGroup |
        QFileDevice::ExeGroup | QFileDevice::ReadOther | QFileDevice::WriteOther |
        QFileDevice::ExeOther;
    return info.isFile() && !(info.permissions() & unsafe);
}

class Driver final : public QObject
{
    Q_OBJECT

public:
    Driver(QGuiApplication& application, const QString& launchFile)
        : m_Application(application), m_Qsf(&application), m_Auth(&application)
    {
        m_Timeout.setSingleShot(true);
        m_Timeout.setInterval(15000);
        connect(&m_Timeout, &QTimer::timeout, this, [this]() {
            fail(QStringLiteral("timed out waiting for TLS launch redemption"));
        });
        m_Auth.attachQsfClient(&m_Qsf);
        m_Auth.setBrokerMediaRouteSink(
            [this](const QString& host, int port) {
                m_MediaInstalled = host == QStringLiteral("192.0.2.44") && port == 47989;
                return m_MediaInstalled;
            },
            [this]() { ++m_MediaClearCalls; });
        m_Auth.setBrokerGameStreamLeaseSink(
            [this](const QString& host, int port, const QString& serverName,
                   const QByteArray& caPem, const QString& audience,
                   const QByteArray& ticket, qint64 expiresAtUtcMs) {
                m_LeaseInstalled = host == QStringLiteral("192.0.2.44") && port == 48123 &&
                    serverName == QStringLiteral("localhost") && !caPem.isEmpty() &&
                    audience == QStringLiteral("vm-100") && ticket == kExpectedTicket &&
                    expiresAtUtcMs > QDateTime::currentMSecsSinceEpoch();
                return m_LeaseInstalled;
            });
        connect(&m_Auth, &SystemAuthClient::sessionEstablished,
                this, &Driver::onEstablished);
        connect(&m_Auth, &SystemAuthClient::lastErrorChanged, this, [this]() {
            if (!m_Auth.lastError().isEmpty() && !m_Auth.authenticated()) {
                fail(m_Auth.lastError());
            }
        });
        m_Timeout.start();
        if (!m_Auth.claimLaunchFile(launchFile)) {
            fail(m_Auth.lastError().isEmpty() ? QStringLiteral("launch descriptor was rejected")
                                              : m_Auth.lastError());
        }
    }

private:
    void onEstablished()
    {
        const QString caPath = SystemAuthClientTestAccess::brokerCaPath(m_Auth);
        if (!require(m_Auth.authenticated() && m_Qsf.hasValidEphemeralSystemAuthTicket(),
                     QStringLiteral("successful redemption did not create an in-memory session")) ||
            !require(m_Qsf.host() == QStringLiteral("192.0.2.44") && m_Qsf.port() == 48122 &&
                     m_Qsf.serverName() == QStringLiteral("localhost") && m_Qsf.caFile() == caPath,
                     QStringLiteral("broker QSF route was not installed exactly")) ||
            !require(m_MediaInstalled && m_LeaseInstalled,
                     QStringLiteral("broker media or lease route sink was not installed")) ||
            !require(!caPath.isEmpty() && isOwnerPrivateFile(caPath),
                     QStringLiteral("broker CA file is absent or not owner-private")) ||
            !require(SystemAuthClientTestAccess::launchStateCleared(m_Auth),
                     QStringLiteral("descriptor claim or CA remained after redemption")) ||
            !require(!settingsContain(QString::fromLatin1(kExpectedTicket)) &&
                     !settingsContain(QStringLiteral("qsd1.")),
                     QStringLiteral("launch credential was persisted in QSettings"))) {
            fail(QStringLiteral("post-redemption security check failed"));
            return;
        }
        m_CaPath = caPath;
        m_ClearCallsBeforeLogout = m_MediaClearCalls;
        m_Auth.logout();
        QTimer::singleShot(0, this, [this]() { onLoggedOut(); });
    }

    void onLoggedOut()
    {
        if (!require(!m_Auth.authenticated() && !m_Qsf.hasValidEphemeralSystemAuthTicket(),
                     QStringLiteral("logout retained the broker ticket")) ||
            !require(m_MediaClearCalls > m_ClearCallsBeforeLogout,
                     QStringLiteral("logout did not clear the broker media route")) ||
            !require(!QFileInfo::exists(m_CaPath),
                     QStringLiteral("ephemeral broker CA file survived logout"))) {
            fail(QStringLiteral("launch cleanup check failed"));
            return;
        }
        m_Timeout.stop();
        QTextStream(stdout) << "QSUNSHINE_LAUNCH_DESCRIPTOR_TLS_E2E_OK\n";
        QTimer::singleShot(0, &m_Application, &QCoreApplication::quit);
    }

    void fail(const QString& message)
    {
        if (m_Finished) {
            return;
        }
        m_Finished = true;
        m_Timeout.stop();
        QTextStream(stderr) << "QSUNSHINE_LAUNCH_DESCRIPTOR_TLS_TEST_FAILED=" << message << '\n';
        QTimer::singleShot(0, &m_Application, [this]() { m_Application.exit(2); });
    }

    QGuiApplication& m_Application;
    QsfClient m_Qsf;
    SystemAuthClient m_Auth;
    QTimer m_Timeout;
    bool m_MediaInstalled = false;
    bool m_LeaseInstalled = false;
    int m_MediaClearCalls = 0;
    int m_ClearCallsBeforeLogout = 0;
    QString m_CaPath;
    bool m_Finished = false;
};

} // namespace

int main(int argc, char* argv[])
{
    QStandardPaths::setTestModeEnabled(true);
    QGuiApplication::setOrganizationName(QStringLiteral("q-sunshine-tests"));
    QGuiApplication::setOrganizationDomain(QStringLiteral("q-sunshine.local"));
    QGuiApplication::setApplicationName(QStringLiteral("qsunshine-launch-descriptor-tls-test"));
    QGuiApplication application(argc, argv);

    QCommandLineParser parser;
    parser.addHelpOption();
    parser.addOption({QStringLiteral("launch-file"), QStringLiteral("descriptor file"),
                      QStringLiteral("path")});
    parser.process(application);
    const QString launchFile = parser.value(QStringLiteral("launch-file")).trimmed();
    if (launchFile.isEmpty()) {
        QTextStream(stderr) << "--launch-file is required\n";
        return 2;
    }
    Driver driver(application, launchFile);
    return application.exec();
}

#include "launchdescriptor_tls_test_main.moc"
