// SPDX-License-Identifier: GPL-3.0-or-later

#include "moonlightcontroller.h"
#include "profilenegotiationcoordinator.h"
#include "qsfclient.h"

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
    // Restore the matching QSF profile before the coordinator can restore a
    // durable profile-settlement guard. During that guard profile switching is
    // correctly refused, so doing this after QML has started would leave a
    // non-default desktop profile paired with QSF's startup-default endpoint.
    qsfClient.selectProfile(moonlight.currentProfileId());
    ProfileNegotiationCoordinator profileNegotiation(&qsfClient, &moonlight, &application);
    QObject::connect(&moonlight, &MoonlightController::streamTeardownStarted,
                     &qsfClient, [&qsfClient]() { qsfClient.setSessionActive(false); });
    QObject::connect(&moonlight, &MoonlightController::streamFinished,
                     &qsfClient, [&qsfClient]() { qsfClient.setSessionActive(false); });
    QObject::connect(&application, &QGuiApplication::aboutToQuit,
                     &qsfClient, [&qsfClient, &moonlight, &profileNegotiation]() {
                         profileNegotiation.cancel();
                         qsfClient.setSessionActive(false);
                         moonlight.cancelPairing();
                         moonlight.stopStream();
                     });

    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty(QStringLiteral("qsfClient"), &qsfClient);
    engine.rootContext()->setContextProperty(QStringLiteral("moonlight"), &moonlight);
    engine.rootContext()->setContextProperty(QStringLiteral("profileNegotiation"),
                                             &profileNegotiation);
    engine.load(QUrl(QStringLiteral("qrc:/Main.qml")));
    if (engine.rootObjects().isEmpty()) {
        return 1;
    }
    return application.exec();
}
