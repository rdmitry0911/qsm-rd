// SPDX-License-Identifier: GPL-3.0-or-later
// Exercise the shipped Qt TLS/PAM client without placing a password in argv.

#include "qsfclient.h"
#include "systemauthclient.h"

#include <QGuiApplication>
#include <QCommandLineParser>
#include <QDateTime>
#include <QSettings>
#include <QStandardPaths>
#include <QTextStream>
#include <QTimer>

// The production interface intentionally has no test-only expiry mutator. The
// friend verifies that the recurring clock guard, rather than a direct test
// call to logout(), closes a ticket/QSF lease when the server-issued expiry is
// now in the past (the same state produced by a forward wall-clock jump).
class SystemAuthClientTestAccess
{
public:
    static void makeTicketPastDueForClockGuard(SystemAuthClient& client)
    {
        client.m_ExpiresAtUtcMs = QDateTime::currentMSecsSinceEpoch() - 1;
        client.m_ClockGuardTimer->start();
    }
};

namespace {

struct Arguments {
    QString authHost;
    int authPort = 0;
    QString authServerName;
    QString authCaFile;
    QString qsfHost;
    int qsfPort = 0;
    QString qsfServerName;
    QString qsfCaFile;
    QString profileId;
    QString expectedAudience;
    QString username;
};

bool require(bool condition, const QString& message)
{
    if (!condition) {
        QTextStream(stderr) << "QSUNSHINE_SYSTEM_AUTH_QT_TEST_FAILED=" << message << '\n';
    }
    return condition;
}

bool checkNoSecretPersistence(const QString& password)
{
    QSettings settings;
    for (const QString& key : settings.allKeys()) {
        const QString loweredKey = key.toLower();
        if (loweredKey.contains(QStringLiteral("password")) ||
            loweredKey.contains(QStringLiteral("ticket")) ||
            loweredKey.contains(QStringLiteral("subject")) ||
            loweredKey.contains(QStringLiteral("expires"))) {
            return false;
        }
        const QString value = settings.value(key).toString();
        if (value.contains(password) || value.contains(QStringLiteral("qsa1."))) {
            return false;
        }
    }
    return true;
}

class Driver final : public QObject
{
    Q_OBJECT

public:
    Driver(QGuiApplication& application, Arguments arguments, QString password)
        : m_Application(application),
          m_Arguments(std::move(arguments)),
          m_Password(std::move(password)),
          m_Qsf(&application),
          m_Auth(&application)
    {
        m_Timeout.setSingleShot(true);
        m_Timeout.setInterval(20000);
        connect(&m_Timeout, &QTimer::timeout, this, [this]() {
            fail(QStringLiteral("timed out waiting for system authentication"));
        });
        m_Qsf.selectProfile(m_Arguments.profileId);
        if (m_Qsf.profileId() != m_Arguments.profileId ||
            !m_Auth.selectProfile(m_Arguments.profileId)) {
            fail(QStringLiteral("could not select the system-auth test profile"));
            return;
        }
        m_Auth.attachQsfClient(&m_Qsf);
        if (!m_Qsf.applyConfiguration(m_Arguments.qsfHost, m_Arguments.qsfPort,
                                      m_Arguments.qsfServerName, m_Arguments.qsfCaFile,
                                      QString(), QString()) ||
            !m_Auth.applyConfiguration(m_Arguments.authHost, m_Arguments.authPort,
                                       m_Arguments.authServerName, m_Arguments.authCaFile,
                                       m_Arguments.expectedAudience)) {
            fail(QStringLiteral("could not configure system-auth test client"));
            return;
        }
        connect(&m_Auth, &SystemAuthClient::authenticatedChanged, this, [this]() {
            if (!m_Auth.authenticated()) {
                if (m_Phase == Phase::WaitingForClockGuardExpiry) {
                    onClockGuardExpired();
                }
                return;
            }
            onAuthenticated();
        });
        connect(&m_Auth, &SystemAuthClient::lastErrorChanged, this, [this]() {
            if (!m_Auth.lastError().isEmpty() && !m_Auth.authenticated()) {
                fail(m_Auth.lastError());
            }
        });
        connect(&m_Qsf, &QsfClient::readyChanged, this, [this]() {
            if (m_Qsf.ready()) {
                onQsfGatewayReady();
            }
        });
        connect(&m_Qsf, &QsfClient::lastErrorChanged, this, [this]() {
            if (!m_Qsf.lastError().isEmpty() && m_Phase != Phase::Complete &&
                m_Phase != Phase::Failed) {
                fail(m_Qsf.lastError());
            }
        });
        m_Timeout.start();
        m_Auth.login(m_Arguments.username, m_Password);
    }

private:
    enum class Phase {
        WaitingForFirstLogin,
        WaitingForSecondLogin,
        WaitingForQsfGateway,
        LoggingOut,
        WaitingForExpiryLogin,
        WaitingForExpiryQsfGateway,
        WaitingForClockGuardExpiry,
        Complete,
        Failed,
    };

    void onAuthenticated()
    {
        if (m_Phase != Phase::WaitingForFirstLogin && m_Phase != Phase::WaitingForSecondLogin &&
            m_Phase != Phase::WaitingForExpiryLogin) {
            return;
        }
        if (!require(m_Auth.subject() == m_Arguments.username,
                     QStringLiteral("unexpected authenticated subject")) ||
            !require(m_Qsf.hasValidEphemeralSystemAuthTicket(),
                     QStringLiteral("QSF did not receive an in-memory ticket"))) {
            fail(QStringLiteral("system-auth post-login checks failed"));
            return;
        }
        if (m_Phase == Phase::WaitingForFirstLogin) {
            // An endpoint/trust change is an audience boundary, even before
            // the QSF lease has been activated. The attached SystemAuthClient
            // must revoke local launch admission rather than leaving its
            // successful PAM result live after QsfClient clears the ticket.
            const int alternatePort = m_Arguments.qsfPort == 65535
                                          ? 65534
                                          : m_Arguments.qsfPort + 1;
            m_Phase = Phase::WaitingForSecondLogin;
            if (!m_Qsf.applyConfiguration(m_Arguments.qsfHost, alternatePort,
                                          m_Arguments.qsfServerName, m_Arguments.qsfCaFile,
                                          QString(), QString()) ||
                !require(!m_Auth.authenticated() &&
                         !m_Qsf.hasValidEphemeralSystemAuthTicket(),
                         QStringLiteral("QSF endpoint change did not clear system-auth admission")) ||
                !m_Qsf.applyConfiguration(m_Arguments.qsfHost, m_Arguments.qsfPort,
                                          m_Arguments.qsfServerName, m_Arguments.qsfCaFile,
                                          QString(), QString())) {
                fail(QStringLiteral("could not enforce or restore the QSF route boundary"));
                return;
            }
            m_Auth.login(m_Arguments.username, m_Password);
            return;
        }
        // This is a real, separate QSF ticket-mode endpoint. Its status reply
        // proves the QsfClient injected the in-memory ticket over TLS, the
        // gateway validated it, and qsf-control reached its local fake agent.
        m_Phase = m_Phase == Phase::WaitingForExpiryLogin
                      ? Phase::WaitingForExpiryQsfGateway
                      : Phase::WaitingForQsfGateway;
        m_Qsf.setSessionActive(true);
        if (!require(m_Qsf.sessionActive(),
                     QStringLiteral("QSF session was not admitted for the ticket gateway"))) {
            fail(QStringLiteral("QSF setup failed"));
        }
    }

    void onQsfGatewayReady()
    {
        if (m_Phase != Phase::WaitingForQsfGateway &&
            m_Phase != Phase::WaitingForExpiryQsfGateway) {
            return;
        }
        if (!require(m_Qsf.sessionActive() && m_Qsf.ready(),
                     QStringLiteral("ticket-mode QSF gateway did not establish a ready lease"))) {
            fail(QStringLiteral("QSF ticket gateway readiness check failed"));
            return;
        }
        if (m_Phase == Phase::WaitingForExpiryQsfGateway) {
            // Do not invoke logout here: make the server-issued expiry appear
            // past due and let SystemAuthClient's recurring wall-clock guard
            // clear the real QSF lease. This covers a forward wall-clock jump
            // without waiting for the original monotonic expiry timer.
            m_Phase = Phase::WaitingForClockGuardExpiry;
            SystemAuthClientTestAccess::makeTicketPastDueForClockGuard(m_Auth);
            return;
        }
        // Logout is synchronous at the local admission boundary. It must
        // cancel the real QSF lease after its accepted status transaction,
        // not merely a pre-dispatch placeholder.
        m_Phase = Phase::LoggingOut;
        m_Auth.logout();
        const bool noPersistence = checkNoSecretPersistence(m_Password);
        if (!require(!m_Auth.authenticated() && !m_Qsf.sessionActive() && !m_Qsf.ready() &&
                     !m_Qsf.hasValidEphemeralSystemAuthTicket(),
                     QStringLiteral("logout did not clear the QSF admission ticket and lease")) ||
            !require(noPersistence,
                     QStringLiteral("password or ticket was persisted through QSettings"))) {
            fail(QStringLiteral("logout/persistence check failed"));
            return;
        }
        // Re-establish a real ticket/QSF lease and exercise the periodic
        // expiry watchdog separately from the explicit logout path above.
        m_Phase = Phase::WaitingForExpiryLogin;
        m_Auth.login(m_Arguments.username, m_Password);
    }

    void onClockGuardExpired()
    {
        if (!require(!m_Auth.authenticated() && !m_Qsf.sessionActive() && !m_Qsf.ready() &&
                     !m_Qsf.hasValidEphemeralSystemAuthTicket(),
                     QStringLiteral("expiry watchdog did not clear the QSF admission ticket and lease")) ||
            !require(checkNoSecretPersistence(m_Password),
                     QStringLiteral("password or ticket was persisted through QSettings after expiry"))) {
            fail(QStringLiteral("expiry watchdog/persistence check failed"));
            return;
        }
        m_Phase = Phase::Complete;
        QTextStream(stdout) << "QSUNSHINE_SYSTEM_AUTH_QT_QSF_E2E_OK\n"
                            << "QSUNSHINE_SYSTEM_AUTH_EXPIRY_GUARD_OK\n"
                            << "QSUNSHINE_SYSTEM_AUTH_QT_CLIENT_OK\n";
        m_Timeout.stop();
        m_Password.fill(QChar(0));
        m_Password.clear();
        QTimer::singleShot(0, &m_Application, &QCoreApplication::quit);
    }

    void fail(const QString& error)
    {
        if (m_Finished) {
            return;
        }
        m_Finished = true;
        m_Phase = Phase::Failed;
        m_Timeout.stop();
        m_Password.fill(QChar(0));
        m_Password.clear();
        QTextStream(stderr) << "QSUNSHINE_SYSTEM_AUTH_QT_TEST_FAILED=" << error << '\n';
        QTimer::singleShot(0, &m_Application, [this]() { m_Application.exit(2); });
    }

    QGuiApplication& m_Application;
    Arguments m_Arguments;
    QString m_Password;
    QsfClient m_Qsf;
    SystemAuthClient m_Auth;
    QTimer m_Timeout;
    Phase m_Phase = Phase::WaitingForFirstLogin;
    bool m_Finished = false;
};

} // namespace

int main(int argc, char* argv[])
{
    QStandardPaths::setTestModeEnabled(true);
    QGuiApplication::setOrganizationName(QStringLiteral("q-sunshine-tests"));
    QGuiApplication::setOrganizationDomain(QStringLiteral("q-sunshine.local"));
    QGuiApplication::setApplicationName(QStringLiteral("qsunshine-system-auth-test"));
    QGuiApplication application(argc, argv);

    QCommandLineParser parser;
    parser.addHelpOption();
    parser.addOption({QStringLiteral("auth-host"), QStringLiteral("system-auth gateway host"), QStringLiteral("host")});
    parser.addOption({QStringLiteral("auth-port"), QStringLiteral("system-auth gateway port"), QStringLiteral("port")});
    parser.addOption({QStringLiteral("auth-server-name"), QStringLiteral("system-auth TLS server name"), QStringLiteral("name")});
    parser.addOption({QStringLiteral("auth-ca-file"), QStringLiteral("system-auth CA PEM"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("qsf-host"), QStringLiteral("ticket-mode QSF gateway host"), QStringLiteral("host")});
    parser.addOption({QStringLiteral("qsf-port"), QStringLiteral("ticket-mode QSF gateway port"), QStringLiteral("port")});
    parser.addOption({QStringLiteral("qsf-server-name"), QStringLiteral("QSF TLS server name"), QStringLiteral("name")});
    parser.addOption({QStringLiteral("qsf-ca-file"), QStringLiteral("QSF CA PEM"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("profile-id"), QStringLiteral("audience/profile identifier"), QStringLiteral("profile")});
    parser.addOption({QStringLiteral("expected-audience"), QStringLiteral("required ticket VM audience"), QStringLiteral("audience")});
    parser.addOption({QStringLiteral("username"), QStringLiteral("allowed test username"), QStringLiteral("username")});
    parser.addOption({QStringLiteral("password-stdin"), QStringLiteral("read the test password from standard input")});
    parser.process(application);
    if (!parser.isSet(QStringLiteral("password-stdin"))) {
        QTextStream(stderr) << "--password-stdin is required\n";
        return 2;
    }
    bool authPortOk = false;
    bool qsfPortOk = false;
    Arguments arguments;
    arguments.authHost = parser.value(QStringLiteral("auth-host")).trimmed();
    arguments.authPort = parser.value(QStringLiteral("auth-port")).toInt(&authPortOk);
    arguments.authServerName = parser.value(QStringLiteral("auth-server-name")).trimmed();
    arguments.authCaFile = parser.value(QStringLiteral("auth-ca-file")).trimmed();
    arguments.qsfHost = parser.value(QStringLiteral("qsf-host")).trimmed();
    arguments.qsfPort = parser.value(QStringLiteral("qsf-port")).toInt(&qsfPortOk);
    arguments.qsfServerName = parser.value(QStringLiteral("qsf-server-name")).trimmed();
    arguments.qsfCaFile = parser.value(QStringLiteral("qsf-ca-file")).trimmed();
    arguments.profileId = parser.value(QStringLiteral("profile-id")).trimmed();
    arguments.expectedAudience = parser.value(QStringLiteral("expected-audience")).trimmed();
    arguments.username = parser.value(QStringLiteral("username")).trimmed();
    QString password = QTextStream(stdin).readLine();
    if (arguments.authHost.isEmpty() || !authPortOk || arguments.authPort < 1 ||
        arguments.authPort > 65535 || arguments.authCaFile.isEmpty() ||
        arguments.qsfHost.isEmpty() || !qsfPortOk || arguments.qsfPort < 1 ||
        arguments.qsfPort > 65535 || arguments.qsfCaFile.isEmpty() ||
        arguments.profileId.isEmpty() || arguments.expectedAudience.isEmpty() ||
        arguments.username.isEmpty() || password.isEmpty()) {
        QTextStream(stderr) << "invalid system-auth test arguments\n";
        return 2;
    }
    Driver driver(application, arguments, std::move(password));
    return application.exec();
}

#include "systemauthclient_test_main.moc"
