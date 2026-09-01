// SPDX-License-Identifier: GPL-3.0-or-later

#include "moonlightcontroller.h"
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
    QObject::connect(&moonlight, &MoonlightController::streamTeardownStarted,
                     &qsfClient, [&qsfClient]() { qsfClient.setSessionActive(false); });
    QObject::connect(&moonlight, &MoonlightController::streamFinished,
                     &qsfClient, [&qsfClient]() { qsfClient.setSessionActive(false); });
    QObject::connect(&application, &QGuiApplication::aboutToQuit,
                     &qsfClient, [&qsfClient, &moonlight]() {
                         qsfClient.setSessionActive(false);
                         moonlight.cancelPairing();
                         moonlight.stopStream();
                     });

    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty(QStringLiteral("qsfClient"), &qsfClient);
    engine.rootContext()->setContextProperty(QStringLiteral("moonlight"), &moonlight);
    engine.load(QUrl(QStringLiteral("qrc:/Main.qml")));
    if (engine.rootObjects().isEmpty()) {
        return 1;
    }
    return application.exec();
}
