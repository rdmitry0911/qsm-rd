// SPDX-License-Identifier: GPL-3.0-or-later

#include "moonlightcontroller.h"
#include "profilenegotiationcoordinator.h"
#include "qsfclient.h"
#include "systemauthclient.h"

#include <QCommandLineParser>
#include <QEvent>
#include <QFileInfo>
#include <QFileOpenEvent>
#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickStyle>
#include <QTextStream>
#include <QTimer>
#include <QUrl>

#include <functional>
#include <utility>

namespace {

// Finder/LaunchServices delivers document opens as QFileOpenEvent after the
// app has been created.  Queue the file until the C++ receiver composition is
// ready, rather than treating an untrusted document path as a QML value.
class LaunchReceiverApplication final : public QGuiApplication
{
public:
    using LaunchFileHandler = std::function<void(const QString&)>;

    LaunchReceiverApplication(int& argc, char** argv)
        : QGuiApplication(argc, argv)
    {
    }

    void setLaunchFileHandler(LaunchFileHandler handler)
    {
        m_LaunchFileHandler = std::move(handler);
        const QStringList pending = std::exchange(m_PendingLaunchFiles, {});
        for (const QString& path : pending) {
            m_LaunchFileHandler(path);
        }
    }

protected:
    bool event(QEvent* event) override
    {
#ifdef Q_OS_MACOS
        if (event != nullptr && event->type() == QEvent::FileOpen) {
            const auto* fileOpenEvent = static_cast<QFileOpenEvent*>(event);
            QString path = fileOpenEvent->file();
            if (path.isEmpty() && fileOpenEvent->url().isLocalFile()) {
                path = fileOpenEvent->url().toLocalFile();
            }
            if (!path.isEmpty()) {
                if (m_LaunchFileHandler) {
                    m_LaunchFileHandler(path);
                }
                else {
                    m_PendingLaunchFiles.append(path);
                }
                return true;
            }
        }
#else
        Q_UNUSED(event);
#endif
        return QGuiApplication::event(event);
    }

private:
    LaunchFileHandler m_LaunchFileHandler;
    QStringList m_PendingLaunchFiles;
};

} // namespace

int main(int argc, char* argv[])
{
    QGuiApplication::setOrganizationName(QStringLiteral("q-sunshine"));
    QGuiApplication::setOrganizationDomain(QStringLiteral("q-sunshine.local"));
    QGuiApplication::setApplicationName(QStringLiteral("q-sunshine-client"));

    LaunchReceiverApplication application(argc, argv);
    QQuickStyle::setStyle(QStringLiteral("Material"));

    QCommandLineParser parser;
    parser.setApplicationDescription(
        QStringLiteral("q-sunshine receiver for a one-use Proxmox VM launch file"));
    parser.addHelpOption();
    const QCommandLineOption launchFileOption(
        QStringList {QStringLiteral("launch-file")},
        QStringLiteral("Redeem the supplied one-use Proxmox .qsm launch file"),
        QStringLiteral("path"));
    parser.addOption(launchFileOption);
    if (!parser.parse(application.arguments())) {
        QTextStream(stderr) << parser.errorText() << '\n';
        return 2;
    }
    if (parser.isSet(QStringLiteral("help"))) {
        parser.showHelp(0);
    }
    const QStringList explicitLaunchFiles = parser.values(launchFileOption);
    if (explicitLaunchFiles.size() > 1 || !parser.positionalArguments().isEmpty()) {
        QTextStream(stderr) << "Usage: qsunshine-client --launch-file /absolute/path/to/launch.qsm\n";
        return 2;
    }
#ifndef Q_OS_MACOS
    if (explicitLaunchFiles.isEmpty()) {
        QTextStream(stderr) << "Usage: qsunshine-client --launch-file /absolute/path/to/launch.qsm\n";
        return 2;
    }
#endif
    // Keep lexical path handling here: SystemAuthClient opens it with
    // O_NOFOLLOW and verifies the opened inode/ownership, rather than
    // canonicalizing a potentially attacker-controlled symlink first.
    const QString launchFilePath = explicitLaunchFiles.isEmpty()
        ? QString() : QFileInfo(explicitLaunchFiles.constFirst()).absoluteFilePath();

    QsfClient qsfClient(&application);
    MoonlightController moonlight(&application);
    SystemAuthClient systemAuth(&application);
    // The ticket is deliberately passed directly between C++ objects.  It is
    // never exposed as a QML property or persisted in QSettings.
    systemAuth.attachQsfClient(&qsfClient);
    // Production media launch is strictly native system-auth mode. The sink
    // keeps the ticket inside C++ and delivers it only to Moonlight's managed
    // stdin pipe when a child is spawned; a stock Moonlight binary rejects the
    // explicit marker rather than silently falling back to PIN pairing.
    moonlight.requireSystemAuthGameStreamLease();
    systemAuth.setBrokerMediaRouteSink(
        [&moonlight](const QString& host, int basePort) {
            return moonlight.setSystemAuthGameStreamMediaRoute(host, basePort);
        },
        [&moonlight]() { moonlight.clearSystemAuthGameStreamLease(); });
    systemAuth.setBrokerGameStreamLeaseSink(
        [&moonlight](const QString& authHost, int authPort, const QString& authServerName,
                     const QByteArray& authCaPem, const QString& audience,
                     const QByteArray& ticket, qint64 expiresAtUtcMs) {
            return moonlight.setSystemAuthGameStreamLeasePem(
                authHost, authPort, authServerName, authCaPem, audience, ticket,
                expiresAtUtcMs);
        });
    // Keep authorization and companion scopes aligned even if a future
    // in-process caller changes the Moonlight profile without passing through
    // the QML helpers.
    QObject::connect(&moonlight, &MoonlightController::profileChanged,
                     &application, [&moonlight, &qsfClient, &systemAuth]() {
                         qsfClient.selectProfile(moonlight.currentProfileId());
                         systemAuth.selectProfile(moonlight.currentProfileId());
                     });
    // Profile names and media hosts are authorization scope, not merely UI
    // labels. Revoke before profile synchronization so an admission cannot
    // be retained by a direct C++ caller that rewrites a profile route.
    QObject::connect(&moonlight, &MoonlightController::streamAuthorizationScopeChanged,
                     &application, [&systemAuth]() { systemAuth.logout(); });
    // Restore the matching QSF profile before the coordinator can restore a
    // durable profile-settlement guard. During that guard profile switching is
    // correctly refused, so doing this after QML has started would leave a
    // non-default desktop profile paired with QSF's startup-default endpoint.
    qsfClient.selectProfile(moonlight.currentProfileId());
    systemAuth.selectProfile(moonlight.currentProfileId());
    QObject::connect(&systemAuth, &SystemAuthClient::authenticatedChanged,
                     &application, [&systemAuth, &moonlight]() {
                         moonlight.setSystemAuthAdmission(systemAuth.authenticated());
                     });
    QObject::connect(&systemAuth, &SystemAuthClient::sessionExplicitlyRevoked,
                     &application, [&moonlight]() {
                         // A deliberate sign-out or a changed authorization
                         // route ends the local stream. Ticket expiry does
                         // not emit this signal: the established Sunshine
                         // mTLS media lease remains valid until it ends.
                         moonlight.cancelPairing();
                         moonlight.stopStream();
                     });
    QObject::connect(&systemAuth, &SystemAuthClient::sessionEstablished,
                     &application, [&systemAuth, &moonlight]() {
                         // Let authenticatedChanged update the C++ admission
                         // gate first.  The descriptor has already installed
                         // only broker-authoritative routes and ephemeral
                         // transport trust at this point.
                         QTimer::singleShot(0, &moonlight, [&systemAuth, &moonlight]() {
                             if (!systemAuth.authenticated() || moonlight.streamBusy()) {
                                 return;
                             }
                             moonlight.startStream(moonlight.profileHost(),
                                                   moonlight.profileAppName(),
                                                   moonlight.profileResolution(),
                                                   moonlight.profileDisplayMode());
                         });
                     });
    moonlight.setSystemAuthAdmission(systemAuth.authenticated());
    ProfileNegotiationCoordinator profileNegotiation(&qsfClient, &moonlight, &application);
    QObject::connect(&moonlight, &MoonlightController::streamTeardownStarted,
                     &qsfClient, [&qsfClient]() { qsfClient.setSessionActive(false); });
    QObject::connect(&moonlight, &MoonlightController::streamFinished,
                     &qsfClient, [&qsfClient]() { qsfClient.setSessionActive(false); });
    QObject::connect(&application, &QGuiApplication::aboutToQuit,
                     &qsfClient, [&qsfClient, &moonlight, &profileNegotiation, &systemAuth]() {
                         profileNegotiation.cancel();
                         // Logout clears the ephemeral QSF ticket and closes
                         // any locally spawned compatibility stream.
                         systemAuth.logout();
                         qsfClient.setSessionActive(false);
                         moonlight.cancelPairing();
                         moonlight.stopStream();
                     });

    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty(QStringLiteral("qsfClient"), &qsfClient);
    engine.rootContext()->setContextProperty(QStringLiteral("moonlight"), &moonlight);
    engine.rootContext()->setContextProperty(QStringLiteral("systemAuth"), &systemAuth);
    engine.rootContext()->setContextProperty(QStringLiteral("profileNegotiation"),
                                             &profileNegotiation);
    engine.load(QUrl(QStringLiteral("qrc:/Main.qml")));
    if (engine.rootObjects().isEmpty()) {
        return 1;
    }
    // Do this after the status shell is visible. A downloaded descriptor is
    // never copied into QML/QSettings and is consumed by SystemAuthClient's
    // strict owner/private-file reader immediately. A supplied CLI file wins
    // over any later Finder event; one receiver process redeems at most one
    // one-use launch claim.
    bool launchClaimStarted = !launchFilePath.isEmpty();
    application.setLaunchFileHandler([&systemAuth, &launchClaimStarted](const QString& path) {
        if (launchClaimStarted) {
            return;
        }
        launchClaimStarted = true;
        const QString absolutePath = QFileInfo(path).absoluteFilePath();
        QTimer::singleShot(0, &systemAuth, [&systemAuth, absolutePath]() {
            systemAuth.claimLaunchFile(absolutePath);
        });
    });
    if (!launchFilePath.isEmpty()) {
        QTimer::singleShot(0, &application, [&systemAuth, launchFilePath]() {
            systemAuth.claimLaunchFile(launchFilePath);
        });
    }
    return application.exec();
}
