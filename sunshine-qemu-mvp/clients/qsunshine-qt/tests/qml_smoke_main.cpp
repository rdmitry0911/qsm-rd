// SPDX-License-Identifier: GPL-3.0-or-later
// Load the shipped QML with the same context objects as qsunshine-client.

#include "moonlightcontroller.h"
#include "profilenegotiationcoordinator.h"
#include "qsfclient.h"
#include "systemauthclient.h"

#include <QDateTime>
#include <QFileInfo>
#include <QGuiApplication>
#include <QMetaProperty>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickItem>
#include <QQuickStyle>
#include <QQuickWindow>
#include <QSettings>
#include <QStandardPaths>
#include <QTextStream>
#include <QTimer>
#include <QUrl>
#include <QVariant>

namespace {

bool require(bool condition, const QString& message)
{
    if (!condition) {
        QTextStream(stderr) << "QSUNSHINE_QML_SMOKE_FAILED=" << message << '\n';
    }
    return condition;
}

bool pageFitsViewport(QQuickItem* page, QQuickItem* scroll)
{
    if (!page || !scroll || page->width() < 0.0 || scroll->width() <= 0.0) {
        return false;
    }
    const QPointF origin = page->mapToItem(scroll, QPointF());
    const qreal availableWidth = scroll->property("availableWidth").toReal();
    constexpr qreal epsilon = 0.5;
    return availableWidth > 0.0 && origin.x() >= -epsilon &&
           origin.x() + page->width() <= availableWidth + epsilon;
}

} // namespace

int main(int argc, char* argv[])
{
    QStandardPaths::setTestModeEnabled(true);
    QGuiApplication::setOrganizationName(QStringLiteral("q-sunshine-tests"));
    QGuiApplication::setOrganizationDomain(QStringLiteral("q-sunshine.local"));
    QGuiApplication::setApplicationName(QStringLiteral("qsunshine-qml-smoke"));

    QGuiApplication application(argc, argv);
    QQuickStyle::setStyle(QStringLiteral("Material"));

    // A stale pre-hardening setting must not select the program that receives
    // a native system-auth ticket. This target intentionally lacks the
    // test-only child override compile definition used by fake-process tests.
    const QString untrustedChild(QStringLiteral("/tmp/q-sunshine-untrusted-child"));
    QSettings settings;
    settings.setValue(QStringLiteral("q-sunshine/client/moonlightBinary"), untrustedChild);
    settings.sync();

    QsfClient qsfClient(&application);
    MoonlightController moonlight(&application);
    SystemAuthClient systemAuth(&application);
    systemAuth.attachQsfClient(&qsfClient);
    QObject::connect(&moonlight, &MoonlightController::profileChanged,
                     &application, [&moonlight, &qsfClient, &systemAuth]() {
                         qsfClient.selectProfile(moonlight.currentProfileId());
                         systemAuth.selectProfile(moonlight.currentProfileId());
                     });
    QObject::connect(&moonlight, &MoonlightController::streamAuthorizationScopeChanged,
                     &application, [&systemAuth]() { systemAuth.logout(); });
    qsfClient.selectProfile(moonlight.currentProfileId());
    systemAuth.selectProfile(moonlight.currentProfileId());
    QObject::connect(&systemAuth, &SystemAuthClient::authenticatedChanged,
                     &application, [&systemAuth, &moonlight]() {
                         moonlight.setSystemAuthAdmission(systemAuth.authenticated());
                     });
    moonlight.setSystemAuthAdmission(systemAuth.authenticated());
    ProfileNegotiationCoordinator profileNegotiation(&qsfClient, &moonlight, &application);
    const int binaryPathPropertyIndex = moonlight.metaObject()->indexOfProperty("binaryPath");
    const QMetaProperty binaryPathProperty = binaryPathPropertyIndex >= 0
        ? moonlight.metaObject()->property(binaryPathPropertyIndex) : QMetaProperty();
    if (!require(binaryPathPropertyIndex >= 0 && !binaryPathProperty.isWritable() &&
                     moonlight.binaryPath() != untrustedChild,
                 QStringLiteral("production Moonlight child path is writable or read from QSettings"))) {
        return 2;
    }
    // qsunshine-qml-smoke is compiled with an absolute, deliberately absent
    // package path (and without QSUNSHINE_TEST_MOONLIGHT_OVERRIDE). Assert
    // absence before asking the production controller to launch, so this test
    // can never accidentally start a host Moonlight program.
    if (!require(!QFileInfo::exists(moonlight.binaryPath()),
                 QStringLiteral("QML smoke's intentionally missing packaged Moonlight child exists"))) {
        return 2;
    }
    if (!require(moonlight.saveProfile(QStringLiteral("production-fail-closed"),
                                       QStringLiteral("127.0.0.1"),
                                       QStringLiteral("Desktop"),
                                       QStringLiteral("1280x720"),
                                       QStringLiteral("windowed")),
                 QStringLiteral("could not create production fail-closed test profile"))) {
        return 2;
    }
    // Mirror the production main() composition closely enough to prove the
    // native lease route itself cannot bypass a missing package-owned child.
    moonlight.requireSystemAuthGameStreamLease();
    moonlight.setSystemAuthGameStreamLease(
        QStringLiteral("auth.vm.example"), 48123, QStringLiteral("auth.vm.example"),
        QStringLiteral("/tmp/q-sunshine-qml-smoke-auth-ca.pem"), QStringLiteral("vm-100"),
        QByteArrayLiteral("qsa1.eyJhdWQiOiJ2bS0xMDAifQ.smoke-signature"),
        QDateTime::currentMSecsSinceEpoch() + 60000);
    moonlight.setSystemAuthAdmission(true);
    moonlight.startStream(moonlight.profileHost(), moonlight.profileAppName(),
                          moonlight.profileResolution(), moonlight.profileDisplayMode());
    if (!require(!moonlight.running() && !moonlight.streamBusy() &&
                     moonlight.lastError().contains(
                         QStringLiteral("Trusted packaged Moonlight executable is unavailable")),
                 QStringLiteral("missing packaged Moonlight child did not fail closed before QProcess launch"))) {
        return 2;
    }

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

    auto* window = qobject_cast<QQuickWindow*>(engine.rootObjects().constFirst());
    auto* connectionScroll = engine.rootObjects().constFirst()->findChild<QQuickItem*>(
        QStringLiteral("connectionScroll"));
    auto* connectionContent = engine.rootObjects().constFirst()->findChild<QQuickItem*>(
        QStringLiteral("connectionContent"));
    auto* companionScroll = engine.rootObjects().constFirst()->findChild<QQuickItem*>(
        QStringLiteral("companionScroll"));
    auto* companionContent = engine.rootObjects().constFirst()->findChild<QQuickItem*>(
        QStringLiteral("companionContent"));
    auto* tabs = engine.rootObjects().constFirst()->findChild<QObject*>(
        QStringLiteral("mainTabs"));
    auto* presentationMode = engine.rootObjects().constFirst()->findChild<QObject*>(
        QStringLiteral("presentationMode"));
    auto* systemUsername = engine.rootObjects().constFirst()->findChild<QObject*>(
        QStringLiteral("systemAuthUsername"));
    auto* systemPassword = engine.rootObjects().constFirst()->findChild<QObject*>(
        QStringLiteral("systemAuthPassword"));
    auto* systemLogin = engine.rootObjects().constFirst()->findChild<QObject*>(
        QStringLiteral("systemAuthLogin"));

    if (!require(window && connectionScroll && connectionContent && companionScroll &&
                     companionContent && tabs && presentationMode && systemUsername &&
                     systemPassword && systemLogin,
                 QStringLiteral("expected responsive page objects were not created"))) {
        return 2;
    }
    if (!require(window->minimumWidth() == 900 && window->minimumHeight() == 620,
                 QStringLiteral("client window minimum geometry unexpectedly changed"))) {
        return 2;
    }

    // Keep the window's right edge stationary while changing x and width.
    // This is the geometry sequence a native left-edge resize delivers.  The
    // content must stay entirely on-screen rather than being centered into a
    // negative viewport coordinate.
    window->setGeometry(400, 100, 1120, 760);
    application.processEvents();
    if (!require(pageFitsViewport(connectionContent, connectionScroll),
                 QStringLiteral("wide connection page overflows its viewport"))) {
        return 2;
    }
    window->setGeometry(620, 100, 900, 760);
    application.processEvents();
    if (!require(pageFitsViewport(connectionContent, connectionScroll),
                 QStringLiteral("left-edge compact resize moved connection content out of view"))) {
        return 2;
    }

    tabs->setProperty("currentIndex", 1);
    application.processEvents();
    if (!require(pageFitsViewport(companionContent, companionScroll),
                 QStringLiteral("left-edge compact resize moved companion content out of view"))) {
        return 2;
    }

    const QVariantList presentationChoices = presentationMode->property("model").toList();
    if (!require(presentationChoices == QVariantList {QStringLiteral("windowed"),
                                                       QStringLiteral("fullscreen")},
                 QStringLiteral("presentation UI exposes a mode other than windowed/fullscreen"))) {
        return 2;
    }
    if (!require(systemPassword->property("echoMode").toInt() != 0,
                 QStringLiteral("system-auth password editor is not masked"))) {
        return 2;
    }

    QTimer::singleShot(0, &application, [&application]() { application.exit(0); });
    return application.exec();
}
