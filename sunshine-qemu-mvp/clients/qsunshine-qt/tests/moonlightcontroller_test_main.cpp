// SPDX-License-Identifier: GPL-3.0-or-later
// Process-level checks for the shell's stock Moonlight CLI boundary.

#include "moonlightcontroller.h"

#include <QCoreApplication>
#include <QElapsedTimer>
#include <QEventLoop>
#include <QFile>
#include <QSaveFile>
#include <QStandardPaths>
#include <QTemporaryDir>
#include <QThread>
#include <QTextStream>

#include <functional>

namespace {

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

bool require(bool condition, const QString& message)
{
    if (!condition) {
        QTextStream(stderr) << "MOONLIGHT_CONTROLLER_TEST_FAILED=" << message << '\n';
    }
    return condition;
}

bool writeExecutable(const QString& path)
{
    QSaveFile file(path);
    if (!file.open(QIODevice::WriteOnly)) {
        return false;
    }
    const QByteArray script = R"SH(#!/bin/sh
printf '%s\n' "$*" >> "$QSUNSHINE_CONTROLLER_TEST_LOG"
if [ "$1" = pair ]; then
  if [ "$5" = slow-pair ]; then
    trap 'exit 0' TERM INT
    while :; do
      sleep 1
    done
  fi
  exit 0
fi
printf '?token=split'
sleep 0.1
printf 'secret\n'
printf 'Authorization: Bearer diagnostics-secret\n'
printf '%s\n' '--pin 9876'
trap 'exit 0' TERM INT
while :; do
  sleep 1
done
)SH";
    if (file.write(script) != script.size() || !file.commit()) {
        return false;
    }
    return QFile::setPermissions(path, QFileDevice::ReadOwner | QFileDevice::WriteOwner |
                                       QFileDevice::ExeOwner);
}

int loggedStreamCount(const QString& log)
{
    int count = 0;
    const QStringList lines = log.split(QLatin1Char('\n'), Qt::SkipEmptyParts);
    for (const QString& line : lines) {
        if (line.startsWith(QStringLiteral("stream "))) {
            ++count;
        }
    }
    return count;
}

} // namespace

int main(int argc, char* argv[])
{
    QStandardPaths::setTestModeEnabled(true);
    QCoreApplication::setOrganizationName(QStringLiteral("q-sunshine-tests"));
    QCoreApplication::setOrganizationDomain(QStringLiteral("q-sunshine.local"));
    QCoreApplication::setApplicationName(QStringLiteral("qsunshine-controller-test"));
    QCoreApplication application(argc, argv);

    QTemporaryDir directory;
    if (!require(directory.isValid(), QStringLiteral("temporary directory could not be created"))) {
        return 2;
    }
    const QString executable = directory.filePath(QStringLiteral("fake-moonlight.sh"));
    const QString logPath = directory.filePath(QStringLiteral("moonlight-argv.log"));
    if (!require(writeExecutable(executable), QStringLiteral("fake Moonlight executable could not be written"))) {
        return 2;
    }
    qputenv("QSUNSHINE_CONTROLLER_TEST_LOG", logPath.toUtf8());

    MoonlightController controller;
    controller.setBinaryPath(executable);
    controller.setBinaryPath(QString());
    if (!require(controller.binaryPath() == executable &&
                     controller.lastError().contains(QStringLiteral("must not be empty")),
                 QStringLiteral("empty Moonlight executable path was accepted or hidden"))) {
        return 2;
    }
    controller.setVideoDecoder(QStringLiteral("software"));
    if (!require(controller.videoDecoder() == QStringLiteral("software"),
                 QStringLiteral("software Moonlight decoder selection was not retained"))) {
        return 2;
    }
    controller.setVideoDecoder(QStringLiteral("unsafe decoder text"));
    if (!require(controller.videoDecoder() == QStringLiteral("software") &&
                     controller.lastError().contains(QStringLiteral("must be auto")),
                 QStringLiteral("invalid Moonlight decoder selection was accepted"))) {
        return 2;
    }
    if (!require(controller.saveProfile(QStringLiteral("controller-e2e"),
                                        QStringLiteral("127.0.0.1"),
                                        QStringLiteral("Desktop"),
                                        QStringLiteral("1280x720"),
                                        QStringLiteral("windowed")),
                 QStringLiteral("desktop profile could not be saved"))) {
        return 2;
    }
    const QString composedProfile = QString::fromUtf8("caf\xc3\xa9");
    const QString decomposedProfile = QString::fromUtf8("cafe\xcc\x81");
    if (!require(controller.saveProfile(composedProfile, QStringLiteral("127.0.0.1"),
                                        QStringLiteral("Desktop"), QStringLiteral("1280x720"),
                                        QStringLiteral("windowed")),
                 QStringLiteral("composed Unicode profile could not be saved")) ||
        !require(controller.saveProfile(decomposedProfile, QStringLiteral("127.0.0.1"),
                                        QStringLiteral("Desktop"), QStringLiteral("1280x720"),
                                        QStringLiteral("windowed")),
                 QStringLiteral("decomposed Unicode profile could not be saved")) ||
        !require(controller.currentProfileId() == composedProfile &&
                     controller.profileIds().count(composedProfile) == 1 &&
                     !controller.profileIds().contains(decomposedProfile),
                 QStringLiteral("canonical-equivalent profile IDs split saved state")) ||
        !require(controller.selectProfile(QStringLiteral("controller-e2e")),
                 QStringLiteral("controller E2E profile could not be reselected"))) {
        return 2;
    }

    int teardownCount = 0;
    int pairProcessStartCount = 0;
    QObject::connect(&controller, &MoonlightController::streamTeardownStarted,
                     &application, [&teardownCount]() { ++teardownCount; });
    QObject::connect(&controller, &MoonlightController::pairProcessStarted,
                     &application, [&pairProcessStartCount]() { ++pairProcessStartCount; });

    controller.startStream(controller.profileHost(), controller.profileAppName(),
                           controller.profileResolution(), controller.profileDisplayMode());
    if (!require(waitUntil([&controller]() { return controller.running(); }, 5000),
                 QStringLiteral("initial Moonlight child did not start"))) {
        return 2;
    }
    if (!require(waitUntil([&logPath]() {
                     QFile log(logPath);
                     return log.open(QIODevice::ReadOnly) &&
                            QString::fromUtf8(log.readAll()).contains(QStringLiteral("stream "));
                 }, 2000),
                 QStringLiteral("initial Moonlight argv was not recorded"))) {
        return 2;
    }
    if (!require(waitUntil([&controller]() {
                     return controller.recentOutput().contains(QStringLiteral("?token=[redacted]")) &&
                            controller.recentOutput().contains(QStringLiteral("Authorization: [redacted]")) &&
                            controller.recentOutput().contains(QStringLiteral("--pin [redacted]"));
                 }, 2000),
                 QStringLiteral("split Moonlight diagnostic secret was not redacted")) ||
        !require(!controller.recentOutput().contains(QStringLiteral("splitsecret")) &&
                     !controller.recentOutput().contains(QStringLiteral("diagnostics-secret")) &&
                     !controller.recentOutput().contains(QStringLiteral("9876")) &&
                     controller.recentOutput().contains(QStringLiteral("Authorization: [redacted]")) &&
                     controller.recentOutput().contains(QStringLiteral("--pin [redacted]")),
                 QStringLiteral("Moonlight diagnostic secret reached the UI"))) {
        return 2;
    }

    controller.startStream(QStringLiteral("127.0.0.1"), QStringLiteral("Desktop"),
                           QStringLiteral("1920x1080"), QStringLiteral("fullscreen"));
    if (!require(teardownCount == 1, QStringLiteral("reconnect did not announce teardown")) ||
        !require(waitUntil([&controller, &logPath]() {
                     QFile log(logPath);
                     if (!log.open(QIODevice::ReadOnly)) {
                         return false;
                     }
                     return controller.running() &&
                            loggedStreamCount(QString::fromUtf8(log.readAll())) >= 2;
                 }, 8000),
                 QStringLiteral("fullscreen reconnect did not launch a second child"))) {
        return 2;
    }

    QFile log(logPath);
    if (!require(log.open(QIODevice::ReadOnly), QStringLiteral("Moonlight argv log cannot be read"))) {
        return 2;
    }
    const QString argumentsLog = QString::fromUtf8(log.readAll());
    const QStringList streamLines = argumentsLog.split(QLatin1Char('\n'), Qt::SkipEmptyParts);
    QString firstStream;
    QString secondStream;
    for (const QString& line : streamLines) {
        if (!line.startsWith(QStringLiteral("stream "))) {
            continue;
        }
        if (firstStream.isEmpty()) {
            firstStream = line;
        }
        else {
            secondStream = line;
        }
    }
    if (!require(firstStream.contains(QStringLiteral("--display-mode windowed")) &&
                 firstStream.contains(QStringLiteral("--resolution 1280x720")) &&
                 firstStream.contains(QStringLiteral("--no-quit-after")) &&
                 firstStream.contains(QStringLiteral("--video-decoder software")) &&
                 firstStream.contains(QStringLiteral("--absolute-mouse")) &&
                 firstStream.contains(QStringLiteral("software -- 127.0.0.1 Desktop")),
                 QStringLiteral("windowed Moonlight argv is incomplete or unsafe")) ||
        !require(secondStream.contains(QStringLiteral("--display-mode fullscreen")) &&
                 secondStream.contains(QStringLiteral("--resolution 1920x1080")) &&
                 secondStream.contains(QStringLiteral("--no-quit-after")),
                 QStringLiteral("fullscreen Moonlight reconnect argv is incomplete or unsafe"))) {
        return 2;
    }

    controller.stopStream();
    if (!require(teardownCount == 2, QStringLiteral("manual disconnect did not announce teardown")) ||
        !require(waitUntil([&controller]() { return !controller.streamBusy(); }, 6000),
                 QStringLiteral("manual Moonlight disconnect did not finish")) ||
        !require(controller.lastError().isEmpty(),
                 QStringLiteral("intentional Moonlight disconnect was reported as an error"))) {
        return 2;
    }

    controller.pair(QStringLiteral("127.0.0.1"), QStringLiteral("4242"));
    if (!require(waitUntil([&pairProcessStartCount]() { return pairProcessStartCount >= 1; }, 5000),
                 QStringLiteral("pairing child did not report QProcess started")) ||
        !require(waitUntil([&controller]() { return !controller.pairing(); }, 5000),
                 QStringLiteral("pairing child did not finish")) ||
        !require(controller.status().contains(QStringLiteral("verify the host is paired")),
                 QStringLiteral("pairing exit code was incorrectly treated as proof of success"))) {
        return 2;
    }
    if (!require(waitUntil([&logPath]() {
                     QFile pairingLog(logPath);
                     return pairingLog.open(QIODevice::ReadOnly) &&
                            QString::fromUtf8(pairingLog.readAll()).contains(
                                QStringLiteral("pair --pin 4242 -- 127.0.0.1"));
                 }, 1000),
                 QStringLiteral("pairing host was not protected by end-of-options"))) {
        return 2;
    }

    // Regression for cancellation generation: the delayed kill scheduled for
    // pairing A must not terminate pairing B after the shared QProcess is
    // reused.
    controller.pair(QStringLiteral("slow-pair"), QStringLiteral("1111"));
    if (!require(waitUntil([&controller]() { return controller.pairing(); }, 1000),
                 QStringLiteral("slow pairing A did not start"))) {
        return 2;
    }
    controller.cancelPairing();
    if (!require(waitUntil([&controller]() { return !controller.pairing(); }, 2000),
                 QStringLiteral("slow pairing A did not cancel"))) {
        return 2;
    }
    controller.pair(QStringLiteral("slow-pair"), QStringLiteral("2222"));
    if (!require(waitUntil([&controller]() { return controller.pairing(); }, 1000),
                 QStringLiteral("slow pairing B did not start"))) {
        return 2;
    }
    QElapsedTimer cancellationTimer;
    cancellationTimer.start();
    if (!require(waitUntil([&cancellationTimer]() { return cancellationTimer.elapsed() >= 3400; }, 4000),
                 QStringLiteral("did not wait for stale pairing A timeout")) ||
        !require(controller.pairing(),
                 QStringLiteral("stale pairing A timeout killed pairing B"))) {
        return 2;
    }
    controller.cancelPairing();
    if (!require(waitUntil([&controller]() { return !controller.pairing(); }, 2000),
                 QStringLiteral("slow pairing B did not cancel"))) {
        return 2;
    }

    MoonlightController reloaded;
    if (!require(reloaded.currentProfileId() == QStringLiteral("controller-e2e") &&
                 reloaded.profileHost() == QStringLiteral("127.0.0.1") &&
                 reloaded.profileResolution() == QStringLiteral("1920x1080") &&
                 reloaded.profileDisplayMode() == QStringLiteral("fullscreen"),
                 QStringLiteral("saved desktop profile was not reloaded"))) {
        return 2;
    }
    QTextStream(stdout) << "MOONLIGHT_CONTROLLER_TEST_OK\n" << Qt::flush;
    return 0;
}
