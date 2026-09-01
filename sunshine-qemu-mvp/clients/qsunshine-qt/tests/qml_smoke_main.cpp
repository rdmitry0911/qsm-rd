// SPDX-License-Identifier: GPL-3.0-or-later
// Load the shipped QML with the same context objects as qsunshine-client.

#include "moonlightcontroller.h"
#include "profilenegotiationcoordinator.h"
#include "qsfclient.h"

#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickStyle>
#include <QStandardPaths>
#include <QTimer>
#include <QUrl>

int main(int argc, char* argv[])
{
    QStandardPaths::setTestModeEnabled(true);
    QGuiApplication::setOrganizationName(QStringLiteral("q-sunshine-tests"));
    QGuiApplication::setOrganizationDomain(QStringLiteral("q-sunshine.local"));
    QGuiApplication::setApplicationName(QStringLiteral("qsunshine-qml-smoke"));

    QGuiApplication application(argc, argv);
    QQuickStyle::setStyle(QStringLiteral("Material"));

    QsfClient qsfClient(&application);
    MoonlightController moonlight(&application);
    ProfileNegotiationCoordinator profileNegotiation(&qsfClient, &moonlight, &application);
    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty(QStringLiteral("qsfClient"), &qsfClient);
    engine.rootContext()->setContextProperty(QStringLiteral("moonlight"), &moonlight);
    engine.rootContext()->setContextProperty(QStringLiteral("profileNegotiation"),
                                             &profileNegotiation);
    engine.load(QUrl(QStringLiteral("qrc:/Main.qml")));
    if (engine.rootObjects().isEmpty()) {
        return 1;
    }
    QTimer::singleShot(0, &application, [&application]() { application.exit(0); });
    return application.exec();
}
