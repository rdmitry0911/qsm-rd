// SPDX-License-Identifier: GPL-3.0-or-later
// Regression for restoring a durable per-desktop-profile settlement guard.

#include "moonlightcontroller.h"
#include "profilenegotiationcoordinator.h"
#include "qsfclient.h"

#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDateTime>
#include <QElapsedTimer>
#include <QFile>
#include <QFileDevice>
#include <QGuiApplication>
#include <QSaveFile>
#include <QSettings>
#include <QStandardPaths>
#include <QTemporaryDir>
#include <QTextStream>
#include <QThread>

#include <functional>

namespace {

constexpr auto kDeadlineKey = "deadline-utc-ms";

bool require(bool condition, const QString& message)
{
    if (!condition) {
        QTextStream(stderr) << "PROFILE_SETTLEMENT_RECOVERY_REGRESSION_FAILED="
                            << message << '\n' << Qt::flush;
    }
    return condition;
}

bool waitUntil(const std::function<bool()>& condition, int timeoutMs)
{
    QElapsedTimer timer;
    timer.start();
    while (!condition()) {
        if (timer.elapsed() >= timeoutMs) {
            return false;
        }
        QCoreApplication::processEvents(QEventLoop::AllEvents, 20);
        QThread::msleep(10);
    }
    return true;
}

bool writeFakeMoonlight(const QString& path)
{
    QSaveFile file(path);
    if (!file.open(QIODevice::WriteOnly)) {
        return false;
    }
    const QByteArray script = R"SH(#!/bin/sh
trap 'exit 0' TERM INT
while :; do
  sleep 1
done
)SH";
    return file.write(script) == script.size() && file.commit() &&
           QFile::setPermissions(path, QFileDevice::ReadOwner | QFileDevice::WriteOwner |
                                       QFileDevice::ExeOwner);
}

QString settlementSettingsGroup(const QString& profileId)
{
    const QByteArray hash = QCryptographicHash::hash(
        profileId.normalized(QString::NormalizationForm_C).toUtf8(),
        QCryptographicHash::Sha256).toHex();
    return QStringLiteral("q-sunshine/client/profile-settlement/") +
           QString::fromLatin1(hash);
}

void setDeadline(const QString& profileId, qint64 deadlineUtcMs)
{
    QSettings settings;
    settings.beginGroup(settlementSettingsGroup(profileId));
    settings.setValue(QString::fromLatin1(kDeadlineKey), deadlineUtcMs);
    settings.endGroup();
    settings.sync();
}

bool deadlineExists(const QString& profileId)
{
    QSettings settings;
    settings.beginGroup(settlementSettingsGroup(profileId));
    const bool exists = settings.contains(QString::fromLatin1(kDeadlineKey));
    settings.endGroup();
    return exists;
}

qint64 utcNowMs()
{
    return QDateTime::currentDateTimeUtc().toMSecsSinceEpoch();
}

} // namespace

int main(int argc, char* argv[])
{
    QStandardPaths::setTestModeEnabled(true);
    QGuiApplication::setOrganizationName(QStringLiteral("q-sunshine-tests"));
    QGuiApplication::setOrganizationDomain(QStringLiteral("q-sunshine.local"));
    QGuiApplication::setApplicationName(QStringLiteral("qsunshine-profile-settlement-recovery-test"));
    QGuiApplication application(argc, argv);

    QSettings settings;
    settings.clear();
    settings.sync();

    QTemporaryDir directory;
    const QString fakeMoonlight = directory.filePath(QStringLiteral("fake-moonlight.sh"));
    if (!require(directory.isValid(), QStringLiteral("temporary directory could not be created")) ||
        !require(writeFakeMoonlight(fakeMoonlight),
                 QStringLiteral("fake Moonlight executable could not be written"))) {
        return 2;
    }

    const QString profileId = QStringLiteral("settlement-recovery-current-profile");
    const QString escapedProfileId = QStringLiteral("settlement-recovery-escaped-profile");
    const QString qsfHost = QStringLiteral("127.0.0.1");
    {
        // Persist the exact current desktop profile first.  The test then
        // writes a marker as a previous process would have done immediately
        // before dispatching connection_optimize.
        MoonlightController bootstrap;
        if (!require(bootstrap.setTestMoonlightBinary(fakeMoonlight),
                     QStringLiteral("fake Moonlight test child could not be selected"))) {
            return 2;
        }
        if (!require(bootstrap.saveProfile(profileId, QStringLiteral("127.0.0.1"),
                                           QStringLiteral("Desktop"),
                                           QStringLiteral("1280x720"),
                                           QStringLiteral("windowed")),
                     QStringLiteral("current desktop profile could not be saved")) ||
            !require(bootstrap.currentProfileId() == profileId,
                     QStringLiteral("saved desktop profile was not made current"))) {
            return 2;
        }

        // The production entry point synchronizes QSF with the restored
        // Moonlight profile before constructing its recovery coordinator.
        // Seed non-default endpoint data so the regression proves that this
        // early synchronization survives the public-operation lock.
        QsfClient qsfBootstrap;
        qsfBootstrap.selectProfile(profileId);
        if (!require(qsfBootstrap.applyConfiguration(qsfHost, 48122,
                                                     QStringLiteral("qsf.test"),
                                                     QStringLiteral("/tmp/qsf-ca.pem"),
                                                     QStringLiteral("/tmp/qsf-cert.pem"),
                                                     QStringLiteral("/tmp/qsf-key.pem")),
                     QStringLiteral("matching QSF profile could not be saved"))) {
            return 2;
        }
    }

    const qint64 liveDeadline = utcNowMs() + 10 * 60 * 1000;
    setDeadline(profileId, liveDeadline);
    if (!require(deadlineExists(profileId),
                 QStringLiteral("pre-construction durable settlement marker is missing"))) {
        return 2;
    }

    {
        MoonlightController moonlight;
        moonlight.setSystemAuthAdmission(true);
        QsfClient qsf;
        qsf.selectProfile(moonlight.currentProfileId());
        ProfileNegotiationCoordinator coordinator(&qsf, &moonlight);

        if (!require(moonlight.currentProfileId() == profileId,
                     QStringLiteral("recovery did not use the current Moonlight profile")) ||
            !require(qsf.profileId() == profileId && qsf.host() == qsfHost,
                     QStringLiteral("QSF was not synchronized to the restored Moonlight profile")) ||
            !require(coordinator.busy(),
                     QStringLiteral("constructor did not restore the live settlement guard")) ||
            !require(coordinator.status().contains(QStringLiteral("previous display-profile transaction")),
                     QStringLiteral("recovery did not report a locked prior transaction"))) {
            return 2;
        }

        int streamStarts = 0;
        QObject::connect(&moonlight, &MoonlightController::streamStarted, &application,
                         [&streamStarts]() { ++streamStarts; });
        moonlight.startStream(moonlight.profileHost(), moonlight.profileAppName(),
                              moonlight.profileResolution(), moonlight.profileDisplayMode());
        QCoreApplication::processEvents(QEventLoop::AllEvents, 20);
        if (!require(streamStarts == 0 && !moonlight.running() &&
                         moonlight.status().contains(QStringLiteral("handoff owns Moonlight start")),
                     QStringLiteral("normal Moonlight start was not blocked by restored guard"))) {
            return 2;
        }

        // The recovery marker is keyed to the selected Moonlight profile.
        // Merely hiding the QML controls is insufficient: a direct caller
        // could otherwise select/save a clean profile, restart, and leave the
        // still-live broker request for this profile behind.  The same guard
        // also freezes decoder/binary choices captured by negotiation.
        const QString originalBinary = moonlight.binaryPath();
        const QString originalDecoder = moonlight.videoDecoder();
        const QString originalHost = moonlight.profileHost();
        if (!require(!moonlight.selectProfile(QStringLiteral("default")) &&
                         moonlight.currentProfileId() == profileId &&
                         moonlight.profileHost() == originalHost &&
                         moonlight.status().contains(QStringLiteral("handoff owns Moonlight desktop profile selection")),
                     QStringLiteral("restored guard allowed selecting a different desktop profile"))) {
            return 2;
        }
        if (!require(!moonlight.saveProfile(escapedProfileId, QStringLiteral("127.0.0.2"),
                                            QStringLiteral("Desktop"),
                                            QStringLiteral("1600x900"),
                                            QStringLiteral("fullscreen")) &&
                         moonlight.currentProfileId() == profileId &&
                         !moonlight.profileIds().contains(escapedProfileId) &&
                         moonlight.profileHost() == originalHost &&
                         moonlight.status().contains(QStringLiteral("handoff owns Moonlight desktop profile changes")),
                     QStringLiteral("restored guard allowed saving a different desktop profile"))) {
            return 2;
        }
        moonlight.setVideoDecoder(QStringLiteral("software"));
        if (!require(moonlight.videoDecoder() == originalDecoder &&
                         moonlight.status().contains(QStringLiteral("handoff owns Moonlight decoder configuration")),
                     QStringLiteral("restored guard allowed changing the decoder"))) {
            return 2;
        }
        moonlight.setTestMoonlightBinary(QStringLiteral("/tmp/unsafe-moonlight"));
        if (!require(moonlight.binaryPath() == originalBinary &&
                         moonlight.status().contains(QStringLiteral("handoff owns Moonlight executable configuration")),
                     QStringLiteral("restored guard allowed changing the Moonlight executable"))) {
            return 2;
        }
        moonlight.pair(QStringLiteral("127.0.0.1"), QStringLiteral("1234"));
        if (!require(!moonlight.pairing() &&
                         moonlight.status().contains(QStringLiteral("handoff owns Moonlight pairing")),
                     QStringLiteral("restored guard allowed a concurrent pairing operation")) ||
            !require(deadlineExists(profileId),
                     QStringLiteral("profile mutations cleared the live recovery marker"))) {
            return 2;
        }

        const QString originalQsfHost = qsf.host();
        const bool originalClipboardSync = qsf.clipboardSyncEnabled();
        qsf.selectProfile(escapedProfileId);
        if (!require(qsf.profileId() == profileId && qsf.host() == originalQsfHost &&
                         qsf.status().contains(QStringLiteral("handoff owns QSF profile selection")),
                     QStringLiteral("restored guard allowed selecting a different QSF profile"))) {
            return 2;
        }
        if (!require(!qsf.applyConfiguration(QStringLiteral("127.0.0.2"), 48123,
                                              QStringLiteral("qsf.other"),
                                              QStringLiteral("/tmp/other-ca.pem"),
                                              QStringLiteral("/tmp/other-cert.pem"),
                                              QStringLiteral("/tmp/other-key.pem")) &&
                         qsf.host() == originalQsfHost &&
                         qsf.status().contains(QStringLiteral("handoff owns QSF endpoint configuration")),
                     QStringLiteral("restored guard allowed changing the QSF endpoint"))) {
            return 2;
        }
        qsf.setClipboardSyncEnabled(!originalClipboardSync);
        if (!require(qsf.clipboardSyncEnabled() == originalClipboardSync &&
                         qsf.status().contains(QStringLiteral("handoff owns QSF clipboard configuration")),
                     QStringLiteral("restored guard allowed changing QSF clipboard configuration"))) {
            return 2;
        }

        qsf.setSessionActive(true);
        if (!require(!qsf.sessionActive() &&
                         qsf.status().contains(QStringLiteral("handoff owns QSF activation")),
                     QStringLiteral("normal QSF activation was not blocked by restored guard"))) {
            return 2;
        }
    }

    // A process restart must reconstruct the same lock from durable settings;
    // profile-switch/save calls above must not provide a persistent bypass.
    {
        MoonlightController moonlight;
        moonlight.setSystemAuthAdmission(true);
        QsfClient qsf;
        qsf.selectProfile(moonlight.currentProfileId());
        ProfileNegotiationCoordinator coordinator(&qsf, &moonlight);
        if (!require(moonlight.currentProfileId() == profileId && qsf.profileId() == profileId &&
                         qsf.host() == qsfHost && coordinator.busy() &&
                         deadlineExists(profileId),
                     QStringLiteral("profile mutation bypassed the durable recovery guard after restart"))) {
            return 2;
        }
    }

    // Do not wait for the production 180-second crash-recovery interval.
    // A fresh app-owned object sees an expired marker, removes it synchronously
    // in its constructor, and must leave normal stream admission available.
    setDeadline(profileId, utcNowMs() - 1);
    {
        MoonlightController moonlight;
        if (!require(moonlight.setTestMoonlightBinary(fakeMoonlight),
                     QStringLiteral("fake Moonlight test child could not be selected after recovery"))) {
            return 2;
        }
        moonlight.setSystemAuthAdmission(true);
        QsfClient qsf;
        qsf.selectProfile(moonlight.currentProfileId());
        ProfileNegotiationCoordinator coordinator(&qsf, &moonlight);

        if (!require(!coordinator.busy(),
                     QStringLiteral("expired settlement marker still locked the coordinator")) ||
            !require(!deadlineExists(profileId),
                     QStringLiteral("expired settlement marker was not cleaned up"))) {
            return 2;
        }

        qsf.setSessionActive(true);
        if (!require(qsf.sessionActive() &&
                         !qsf.status().contains(QStringLiteral("handoff owns QSF activation")) &&
                         !qsf.lastError().isEmpty(),
                     QStringLiteral("expired guard still intercepted normal QSF activation"))) {
            return 2;
        }

        int streamStarts = 0;
        QObject::connect(&moonlight, &MoonlightController::streamStarted, &application,
                         [&streamStarts]() { ++streamStarts; });
        moonlight.startStream(moonlight.profileHost(), moonlight.profileAppName(),
                              moonlight.profileResolution(), moonlight.profileDisplayMode());
        if (!require(waitUntil([&moonlight]() { return moonlight.running(); }, 5000) &&
                         streamStarts == 1,
                     QStringLiteral("expired guard did not release normal Moonlight start"))) {
            moonlight.stopStream();
            return 2;
        }
        moonlight.stopStream();
        if (!require(waitUntil([&moonlight]() { return !moonlight.streamBusy(); }, 5000),
                     QStringLiteral("fake Moonlight child did not stop"))) {
            return 2;
        }
    }

    QTextStream(stdout) << "PROFILE_SETTLEMENT_RECOVERY_REGRESSION_OK\n" << Qt::flush;
    return 0;
}
