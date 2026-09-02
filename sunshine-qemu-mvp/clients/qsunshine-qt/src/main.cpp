// SPDX-License-Identifier: GPL-3.0-or-later

#include "moonlightcontroller.h"
#include "profilenegotiationcoordinator.h"
#include "qsfclient.h"
#include "systemauthclient.h"

#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickStyle>

int main(int argc, char* argv[])
{
    QGuiApplication::setOrganizationName(QStringLiteral("q-sunshine"));
    QGuiApplication::setOrganizationDomain(QStringLiteral("q-sunshine.local"));
    QGuiApplication::setApplicationName(QStringLiteral("q-sunshine-client"));

    QGuiApplication application(argc, argv);
    QQuickStyle::setStyle(QStringLiteral("Material"));

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
    systemAuth.setGameStreamLeaseSink(
        [&moonlight](const QString& authHost, int authPort, const QString& authServerName,
                     const QString& authCaFile, const QString& audience,
                     const QByteArray& ticket, qint64 expiresAtUtcMs) {
            moonlight.setSystemAuthGameStreamLease(authHost, authPort, authServerName,
                                                   authCaFile, audience, ticket,
                                                   expiresAtUtcMs);
        },
        [&moonlight]() { moonlight.clearSystemAuthGameStreamLease(); });
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
    return application.exec();
}
