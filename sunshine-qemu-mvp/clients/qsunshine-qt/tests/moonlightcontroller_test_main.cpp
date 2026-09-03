// SPDX-License-Identifier: GPL-3.0-or-later
// Process-level checks for the shell's Moonlight CLI boundary.

#include "moonlightcontroller.h"

#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDateTime>
#include <QElapsedTimer>
#include <QEventLoop>
#include <QFile>
#include <QSaveFile>
#include <QRegularExpression>
#include <QSettings>
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
if [ "$1" = stream ] && [ "$2" = --qsm-system-auth ]; then
  # The media ticket must arrive only through the inherited one-shot stdin
  # pipe. Deliberately do not log it: the test below proves it cannot leak
  # into argv or the checked environment diagnostics.
  IFS= read -r _qsm_ticket || true
  if [ -n "$_qsm_ticket" ]; then
    printf '%s\n' NATIVE_TICKET_PIPE_OK >> "$QSUNSHINE_CONTROLLER_TEST_LOG"
  fi
  printf 'NATIVE_ENV auth=%s:%s sni=%s ca=%s audience=%s fd=%s host=%s https=%s\n' \
    "$QSM_GAMESTREAM_AUTH_HOST" "$QSM_GAMESTREAM_AUTH_PORT" \
    "$QSM_GAMESTREAM_AUTH_SNI" "$QSM_GAMESTREAM_AUTH_CA_FILE" \
    "$QSM_GAMESTREAM_AUDIENCE" "$QSM_GAMESTREAM_TICKET_FD" \
    "$QSM_GAMESTREAM_HOST" "$QSM_GAMESTREAM_HTTPS_PORT" \
    >> "$QSUNSHINE_CONTROLLER_TEST_LOG"
fi
printf '?token=split'
sleep 0.1
printf 'secret\n'
printf 'Authorization: Bearer diagnostics-secret\n'
printf '%s\n' '--pin 9876'
# Keep this split across process-output chunks too: an arbitrary child that
# echoes the bearer syntax must not disclose a valid-looking qsa1 ticket in
# the shell diagnostics pane.
printf 'system-ticket=qsa1.eyJhdWQiOiJ2bS0xMDAiLCJzdWIiOiJh'
sleep 0.1
printf 'bGljZSJ9.ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno123456\n'
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
    QSettings settings;
    settings.clear();
    settings.sync();

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
    if (!require(!controller.canStartStream(),
                 QStringLiteral("a fresh controller unexpectedly bypassed system-auth admission"))) {
        return 2;
    }
    // The process-contract fixture deliberately models a verified in-memory
    // system session. Real GUI composition obtains this only from
    // SystemAuthClient after a TLS/PAM login.
    controller.setSystemAuthAdmission(true);
    if (!require(controller.canStartStream(),
                 QStringLiteral("test system-auth admission did not unlock the controller"))) {
        return 2;
    }
    if (!require(controller.setTestMoonlightBinary(executable),
                 QStringLiteral("test Moonlight executable override was rejected")) ||
        !require(!controller.setTestMoonlightBinary(QString()) &&
                     controller.binaryPath() == executable &&
                     controller.lastError().contains(QStringLiteral("absolute and non-empty")),
                 QStringLiteral("empty test Moonlight executable override was accepted or hidden"))) {
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
    int authorizationScopeChangeCount = 0;
    QObject::connect(&controller, &MoonlightController::streamTeardownStarted,
                     &application, [&teardownCount]() { ++teardownCount; });
    QObject::connect(&controller, &MoonlightController::pairProcessStarted,
                     &application, [&pairProcessStartCount]() { ++pairProcessStartCount; });
    QObject::connect(&controller, &MoonlightController::streamAuthorizationScopeChanged,
                     &application, [&authorizationScopeChangeCount]() {
                         ++authorizationScopeChangeCount;
                     });

    // A direct caller cannot turn a live system-auth admission into a launch
    // for an arbitrary host. It must first save the route, which emits the
    // composition-root revocation signal.
    controller.startStream(QStringLiteral("127.0.0.2"), controller.profileAppName(),
                           controller.profileResolution(), controller.profileDisplayMode());
    if (!require(!controller.running() &&
                     controller.lastError().contains(QStringLiteral("Save the requested Sunshine host")),
                 QStringLiteral("direct stream launch bypassed the saved authorization route"))) {
        return 2;
    }
    const int scopeChangesBeforeHostRewrite = authorizationScopeChangeCount;
    if (!require(controller.saveProfile(controller.currentProfileId(), QStringLiteral("127.0.0.2"),
                                        controller.profileAppName(), controller.profileResolution(),
                                        controller.profileDisplayMode()) &&
                     authorizationScopeChangeCount == scopeChangesBeforeHostRewrite + 1,
                 QStringLiteral("changing a saved Sunshine host did not announce an authorization-scope change")) ||
        !require(controller.saveProfile(controller.currentProfileId(), QStringLiteral("127.0.0.1"),
                                        controller.profileAppName(), controller.profileResolution(),
                                        controller.profileDisplayMode()) &&
                     authorizationScopeChangeCount == scopeChangesBeforeHostRewrite + 2,
                 QStringLiteral("restoring a saved Sunshine host did not announce an authorization-scope change"))) {
        return 2;
    }

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
                            controller.recentOutput().contains(QStringLiteral("--pin [redacted]")) &&
                            controller.recentOutput().contains(
                                QStringLiteral("system-ticket=[redacted]"));
                 }, 2000),
                 QStringLiteral("split Moonlight diagnostic secret was not redacted")) ||
        !require(!controller.recentOutput().contains(QStringLiteral("splitsecret")) &&
                     !controller.recentOutput().contains(QStringLiteral("diagnostics-secret")) &&
                     !controller.recentOutput().contains(QStringLiteral("9876")) &&
                     !controller.recentOutput().contains(
                         QStringLiteral("qsa1.eyJhdWQiOiJ2bS0xMDAiLCJzdWIiOiJhbGljZSJ9.")) &&
                     controller.recentOutput().contains(QStringLiteral("Authorization: [redacted]")) &&
                     controller.recentOutput().contains(QStringLiteral("--pin [redacted]")) &&
                     controller.recentOutput().contains(
                         QStringLiteral("system-ticket=[redacted]")),
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

    // A QSF connection profile changes QEMU's scanout and must be applied
    // only after its previous Moonlight/Sunshine capture has retired.  The
    // production ProfileNegotiationCoordinator performs this stop; retain the
    // controller contract explicitly here rather than allowing an unsafe
    // active-stream restart.
    if (!require(!controller.applyNegotiatedProfile(2560, 1440, 60, 28000,
                                                    QStringLiteral("H.264")) &&
                     controller.lastError().contains(QStringLiteral("stop")),
                 QStringLiteral("negotiated profile was accepted while a stream was active"))) {
        return 2;
    }
    controller.stopStream();
    if (!require(teardownCount == 2, QStringLiteral("pre-profile stop did not announce teardown")) ||
        !require(waitUntil([&controller]() { return !controller.streamBusy(); }, 6000),
                 QStringLiteral("pre-profile Moonlight stop did not finish")) ||
        !require(controller.applyNegotiatedProfile(2560, 1440, 60, 28000,
                                                   QStringLiteral("H.264")),
                 QStringLiteral("host-selected profile was rejected after stream retirement")) ||
        !require(waitUntil([&controller, &logPath]() {
                     QFile profileLog(logPath);
                     if (!profileLog.open(QIODevice::ReadOnly)) {
                         return false;
                     }
                     return controller.running() &&
                            loggedStreamCount(QString::fromUtf8(profileLog.readAll())) >= 3;
                 }, 8000),
                 QStringLiteral("host-selected profile did not launch after stream retirement"))) {
        return 2;
    }
    if (!require(controller.profileResolution() == QStringLiteral("2560x1440") &&
                 controller.profileFps() == 60 && controller.profileBitrateKbps() == 28000 &&
                 controller.profileVideoCodec() == QStringLiteral("H.264"),
                 QStringLiteral("host-selected profile was not retained by the controller"))) {
        return 2;
    }
    log.close();
    if (!require(log.open(QIODevice::ReadOnly), QStringLiteral("Moonlight argv log cannot be reopened"))) {
        return 2;
    }
    const QStringList optimizedLines = QString::fromUtf8(log.readAll()).split(
        QLatin1Char('\n'), Qt::SkipEmptyParts);
    QString thirdStream;
    for (const QString& line : optimizedLines) {
        if (line.startsWith(QStringLiteral("stream "))) {
            thirdStream = line;
        }
    }
    if (!require(thirdStream.contains(QStringLiteral("--resolution 2560x1440")) &&
                 thirdStream.contains(QStringLiteral("--fps 60")) &&
                 thirdStream.contains(QStringLiteral("--bitrate 28000")) &&
                 thirdStream.contains(QStringLiteral("--video-codec H.264")) &&
                 thirdStream.contains(QStringLiteral("-- 127.0.0.1 Desktop")),
                 QStringLiteral("host-selected Moonlight arguments are incomplete or unsafe"))) {
        return 2;
    }
    controller.stopStream();
    if (!require(teardownCount == 3, QStringLiteral("manual disconnect did not announce teardown")) ||
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

    // The shell intentionally has only two user-facing presentation modes.
    // Do not let a stale third Moonlight CLI mode re-enter a newly saved
    // profile, while keeping profiles written by older q-sunshine versions
    // launchable after upgrade.
    if (!require(!controller.saveProfile(QStringLiteral("controller-e2e"),
                                         QStringLiteral("127.0.0.1"),
                                         QStringLiteral("Desktop"),
                                         QStringLiteral("2560x1440"),
                                         QStringLiteral("borderless")) &&
                     controller.lastError().contains(
                         QStringLiteral("windowed or fullscreen")),
                 QStringLiteral("legacy third presentation mode was accepted"))) {
        return 2;
    }
    const QString profileId = QStringLiteral("controller-e2e");
    const QString profileHash = QString::fromLatin1(
        QCryptographicHash::hash(profileId.toUtf8(), QCryptographicHash::Sha256).toHex());
    settings.beginGroup(QStringLiteral("q-sunshine/client/profiles/") + profileHash);
    settings.setValue(QStringLiteral("displayMode"), QStringLiteral("borderless"));
    settings.endGroup();
    settings.sync();

    MoonlightController reloaded;
    if (!require(reloaded.currentProfileId() == QStringLiteral("controller-e2e") &&
                 reloaded.profileHost() == QStringLiteral("127.0.0.1") &&
                 reloaded.profileResolution() == QStringLiteral("2560x1440") &&
                 reloaded.profileDisplayMode() == QStringLiteral("fullscreen") &&
                 reloaded.profileFps() == 60 && reloaded.profileBitrateKbps() == 28000 &&
                 reloaded.profileVideoCodec() == QStringLiteral("H.264"),
                 QStringLiteral("saved desktop profile was not reloaded"))) {
        return 2;
    }

    // Production composition permanently selects the lease-aware Moonlight
    // path. Verify that its marker, public routing metadata, and one-shot
    // stdin ticket pipe reach the child while the ticket itself reaches
    // neither argv nor the child environment diagnostics.
    controller.requireSystemAuthGameStreamLease();
    controller.pair(QStringLiteral("127.0.0.1"), QStringLiteral("4242"));
    if (!require(!controller.pairing() &&
                 controller.lastError().contains(QStringLiteral("PIN pairing is disabled")),
                 QStringLiteral("native controller unexpectedly retained a PIN pairing fallback"))) {
        return 2;
    }
    const QByteArray nativeTicket("qsa1.controller-native-ticket");
    const QByteArray nativeCaPem("-----BEGIN CERTIFICATE-----\n"
                                 "q-sunshine-controller-test-public-ca\n"
                                 "-----END CERTIFICATE-----\n");
    if (!require(controller.setSystemAuthGameStreamLeasePem(
                     QStringLiteral("auth.vm.example"), 48123,
                     QStringLiteral("auth.vm.example"), nativeCaPem,
                     QStringLiteral("vm-100"), nativeTicket,
                     QDateTime::currentMSecsSinceEpoch() + 60000),
                 QStringLiteral("ephemeral native Moonlight CA file could not be installed"))) {
        return 2;
    }
    const QString savedHostBeforeBrokerLaunch = controller.profileHost();
    settings.beginGroup(QStringLiteral("q-sunshine/client/profiles/") + profileHash);
    const QString persistedHostBeforeBrokerLaunch = settings.value(QStringLiteral("host")).toString();
    settings.endGroup();
    if (!require(controller.setSystemAuthGameStreamMediaRoute(
                     QStringLiteral("192.0.2.44"), 47989),
                 QStringLiteral("broker media route could not be installed"))) {
        return 2;
    }
    if (!require(controller.canStartStream(),
                 QStringLiteral("current native system-auth lease did not admit a stream"))) {
        return 2;
    }
    controller.startStream(controller.profileHost(), controller.profileAppName(),
                           controller.profileResolution(), controller.profileDisplayMode());
    if (!require(waitUntil([&controller, &logPath]() {
                     QFile nativeLog(logPath);
                     if (!nativeLog.open(QIODevice::ReadOnly)) {
                         return false;
                     }
                     const QString output = QString::fromUtf8(nativeLog.readAll());
                     return controller.running() && output.contains(QStringLiteral("NATIVE_TICKET_PIPE_OK")) &&
                            output.contains(QStringLiteral("NATIVE_ENV auth=auth.vm.example:48123"));
                 }, 5000),
                 QStringLiteral("native Moonlight launch did not receive its system-auth ticket pipe"))) {
        return 2;
    }
    QFile nativeLog(logPath);
    if (!require(nativeLog.open(QIODevice::ReadOnly),
                 QStringLiteral("native Moonlight log cannot be read"))) {
        return 2;
    }
    const QString nativeOutput = QString::fromUtf8(nativeLog.readAll());
    const QRegularExpression nativeEnvironmentPattern(
        QStringLiteral("NATIVE_ENV auth=auth\\.vm\\.example:48123 sni=auth\\.vm\\.example "
                       "ca=([^ ]+) audience=vm-100 fd=0 host=192\\.0\\.2\\.44 https=47984"));
    const QRegularExpressionMatch nativeEnvironment = nativeEnvironmentPattern.match(nativeOutput);
    const QString nativeCaPath = nativeEnvironment.captured(1);
    const QFileInfo nativeCaInfo(nativeCaPath);
    const QFileDevice::Permissions unsafeCaPermissions =
        QFileDevice::ReadGroup | QFileDevice::WriteGroup | QFileDevice::ExeGroup |
        QFileDevice::ReadOther | QFileDevice::WriteOther | QFileDevice::ExeOther;
    QFile nativeCaFile(nativeCaPath);
    const bool nativeCaContentsMatch = nativeCaFile.open(QIODevice::ReadOnly) &&
        nativeCaFile.readAll() == nativeCaPem;
    settings.beginGroup(QStringLiteral("q-sunshine/client/profiles/") + profileHash);
    const QString persistedHostAfterBrokerLaunch = settings.value(QStringLiteral("host")).toString();
    settings.endGroup();
    if (!require(nativeOutput.contains(QStringLiteral("stream --qsm-system-auth")) &&
                 nativeEnvironment.hasMatch() && nativeCaInfo.isFile() &&
                 !(nativeCaInfo.permissions() & unsafeCaPermissions) && nativeCaContentsMatch &&
                 controller.profileHost() == savedHostBeforeBrokerLaunch &&
                 controller.profileAppName() == QStringLiteral("QEMU Console") &&
                 persistedHostAfterBrokerLaunch == persistedHostBeforeBrokerLaunch &&
                 nativeOutput.contains(QStringLiteral("192.0.2.44:47989 QEMU Console")) &&
                 !nativeOutput.contains(QString::fromUtf8(nativeTicket)),
                 QStringLiteral("native Moonlight launch leaked its ticket, lost trusted lease metadata, or accepted a caller-selected app"))) {
        return 2;
    }
    // A system-auth ticket is launch admission only.  Model its expiry by
    // withdrawing both controller admission and the one-shot ticket while
    // the child is alive: neither action may interrupt the mTLS media lease
    // that native Sunshine has already accepted.
    controller.setSystemAuthAdmission(false);
    controller.clearSystemAuthGameStreamLease();
    if (!require(!controller.canStartStream() && controller.running() && controller.streamBusy() &&
                 QFileInfo::exists(nativeCaPath),
                 QStringLiteral("ticket expiry incorrectly interrupted an established native stream"))) {
        return 2;
    }
    controller.stopStream();
    if (!require(waitUntil([&controller, &nativeCaPath]() {
                     return !controller.streamBusy() && !QFileInfo::exists(nativeCaPath);
                 }, 6000),
                 QStringLiteral("native Moonlight child did not stop"))) {
        return 2;
    }
    QTextStream(stdout) << "MOONLIGHT_CONTROLLER_TEST_OK\n" << Qt::flush;
    return 0;
}
