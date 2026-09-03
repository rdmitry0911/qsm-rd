// SPDX-License-Identifier: GPL-3.0-or-later

#include "moonlightcontroller.h"

#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileDevice>
#include <QFileInfo>
#include <QProcessEnvironment>
#include <QRegularExpression>
#include <QSettings>
#include <QTimer>
#include <QTemporaryFile>
#include <QUrl>

#ifndef QSUNSHINE_PACKAGED_MOONLIGHT_PATH
#define QSUNSHINE_PACKAGED_MOONLIGHT_PATH "/usr/lib/q-sunshine-client/Moonlight/moonlight"
#endif

namespace {

constexpr int kMaxOutputCharacters = 8192;
constexpr int kMaxOutputLineBytes = 64 * 1024;
constexpr int kReconnectGracePeriodMs = 500;
constexpr qint64 kMaxSystemAuthCaBytes = 64 * 1024;
// The Proxmox transport exposes one opaque VM-console endpoint rather than
// Sunshine's application launcher.  Keep this name in one place because it
// is part of the pinned GameStream wire contract (/applist -> /launch).
const QString kQemuConsoleApplication = QStringLiteral("QEMU Console");

QString boundedText(QString value)
{
    value.replace(QLatin1Char('\r'), QLatin1Char(' '));
    return value.trimmed().left(500);
}

bool hasControlCharacter(const QString& value)
{
    for (const QChar character : value) {
        // QChar has no isControl() member in Qt 6.4.  Unicode Cc is the
        // precise category we want to reject here (in addition to NUL).
        if (character.isNull() || character.category() == QChar::Other_Control) {
            return true;
        }
    }
    return false;
}

QString normalizedProfileId(QString value)
{
    return value.trimmed().normalized(QString::NormalizationForm_C);
}

bool isSupportedDisplayMode(const QString& value)
{
    return value == QStringLiteral("fullscreen") || value == QStringLiteral("windowed");
}

QString normalizeDisplayMode(QString value)
{
    // q-sunshine deliberately exposes only the two presentation choices that
    // have a clear, portable meaning in stock Moonlight.  Older releases
    // persisted Moonlight's third "borderless" option.  Treat that legacy
    // value as fullscreen on load so an upgrade neither leaves a profile
    // unlaunchable nor silently turns an immersive presentation into a
    // decorated window.
    if (value == QStringLiteral("borderless")) {
        return QStringLiteral("fullscreen");
    }
    return value;
}

bool isSupportedVideoDecoder(const QString& value)
{
    return value == QStringLiteral("auto") || value == QStringLiteral("software") ||
           value == QStringLiteral("hardware");
}

bool isSupportedStreamVideoCodec(const QString& value)
{
    return value == QStringLiteral("auto") || value == QStringLiteral("H.264") ||
           value == QStringLiteral("HEVC") || value == QStringLiteral("AV1");
}

bool isSupportedResolution(const QString& value)
{
    static const QRegularExpression pattern(QStringLiteral("\\A([0-9]{2,5})x([0-9]{2,5})\\z"));
    const QRegularExpressionMatch match = pattern.match(value);
    if (!match.hasMatch()) {
        return false;
    }
    bool widthOk = false;
    bool heightOk = false;
    const int width = match.captured(1).toInt(&widthOk);
    const int height = match.captured(2).toInt(&heightOk);
    return widthOk && heightOk && width >= 64 && width <= 16384 &&
           height >= 64 && height <= 16384;
}

QString redactMoonlightOutput(QString output)
{
    // Moonlight normally redacts secrets itself. Keep the diagnostics pane
    // defensive in case a future build prints launch URLs or credentials.
    static const QRegularExpression querySecret(
        QStringLiteral("(?i)([?&](?:rikey|rikeyid|token|authorization|access[_-]?token|refresh[_-]?token|pin|password|secret)=)[^&\\s]+"));
    static const QRegularExpression headerSecret(
        QStringLiteral("(?i)((?:authorization|proxy-authorization|x-api-key|cookie):)[^\\r\\n]*"));
    static const QRegularExpression pinSecret(
        QStringLiteral("(?i)(--pin(?:=|\\s+)|\\bpin\\s*[:=]\\s*)\\S+"));
    static const QRegularExpression systemAuthTicket(
        QStringLiteral("(?<![A-Za-z0-9_-])qsa1\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+(?![A-Za-z0-9_-])"));
    output.replace(querySecret, QStringLiteral("\\1[redacted]"));
    output.replace(headerSecret, QStringLiteral("\\1 [redacted]"));
    output.replace(pinSecret, QStringLiteral("\\1[redacted]"));
    output.replace(systemAuthTicket, QStringLiteral("[redacted]"));
    return output;
}

QString packagedMoonlightBinary()
{
#ifdef Q_OS_MACOS
    // The Tahoe bundle creates and signs this exact nested executable. There
    // is deliberately no lowercase compatibility candidate or PATH fallback:
    // a stock child must fail closed before it can consume a media ticket.
    return QDir::cleanPath(QDir(QCoreApplication::applicationDirPath()).absoluteFilePath(
        QStringLiteral("../Resources/Moonlight.app/Contents/MacOS/Moonlight")));
#else
    // Linux desktop packaging installs the pinned patched child at this
    // compile-time libexec location. A development build without that child
    // refuses to launch rather than resolving an arbitrary PATH program.
    return QStringLiteral(QSUNSHINE_PACKAGED_MOONLIGHT_PATH);
#endif
}

} // namespace

MoonlightController::MoonlightController(QObject* parent)
    : QObject(parent),
      m_Running(false),
      m_Pairing(false),
      m_Status(QStringLiteral("Moonlight is idle")),
      m_DiscardingOutputLine(false),
      m_StreamProcess(new QProcess(this)),
      m_PairProcess(new QProcess(this)),
      m_StopTimer(new QTimer(this)),
      m_HasPendingRestart(false),
      m_StopRequested(false),
      m_ProfileHandoffStartBlocked(false),
      m_SystemAuthAdmission(false),
      m_SystemAuthGameStreamLeaseRequired(false),
      m_SystemAuthPort(0),
      m_SystemAuthTicketExpiresAtUtcMs(0),
      m_PairCancelRequested(false),
      m_PairGeneration(0),
      m_RestartGeneration(0),
      m_PendingRestartGeneration(0),
      m_RemoveEphemeralSystemAuthCaWhenStopped(false)
{
    QSettings settings;
    // Do not read the legacy moonlightBinary setting. It is intentionally
    // inert after upgrade: a per-user setting must never choose the process
    // that receives the native system-auth bearer ticket.
    m_BinaryPath = packagedMoonlightBinary();
    m_VideoDecoder = settings.value(QStringLiteral("q-sunshine/client/videoDecoder"),
                                    QStringLiteral("auto")).toString().trimmed();
    if (!isSupportedVideoDecoder(m_VideoDecoder)) {
        m_VideoDecoder = QStringLiteral("auto");
    }

    const QStringList storedProfileIds =
        settings.value(QStringLiteral("q-sunshine/client/profileIds")).toStringList();
    for (const QString& storedProfileId : storedProfileIds) {
        QString error;
        const QString normalized = normalizedProfileId(storedProfileId);
        if (validProfileId(normalized, &error) && !m_ProfileIds.contains(normalized)) {
            m_ProfileIds.append(normalized);
        }
    }
    if (m_ProfileIds.isEmpty()) {
        m_ProfileIds.append(QStringLiteral("default"));
    }
    const QString requestedProfileId =
        settings.value(QStringLiteral("q-sunshine/client/currentProfileId"),
                       m_ProfileIds.constFirst()).toString();
    const QString normalizedRequestedProfileId = normalizedProfileId(requestedProfileId);
    m_CurrentProfileId = m_ProfileIds.contains(normalizedRequestedProfileId)
        ? normalizedRequestedProfileId : m_ProfileIds.constFirst();
    loadProfile(m_CurrentProfileId);

    m_StreamProcess->setProcessChannelMode(QProcess::MergedChannels);
    // In native system-auth mode fd 0 is a one-shot pipe containing only the
    // current bearer ticket. The patched Moonlight process consumes and closes
    // it before it starts its UI; default managed input gives us that pipe
    // without creating a temporary file or placing the secret in argv/env.
    m_StreamProcess->setInputChannelMode(QProcess::ManagedInputChannel);
    connect(m_StreamProcess, &QProcess::readyRead, this, [this]() {
        appendOutput(m_StreamProcess->readAll());
    });
    connect(m_StreamProcess, &QProcess::started, this, [this]() {
        if (m_SystemAuthGameStreamLeaseRequired) {
            if (m_SystemAuthTicket.isEmpty() ||
                m_SystemAuthTicketExpiresAtUtcMs <= QDateTime::currentMSecsSinceEpoch()) {
                setLastError(QStringLiteral("System-auth media ticket expired before Moonlight started"));
                m_StreamProcess->kill();
                return;
            }
            const qint64 expected = m_SystemAuthTicket.size();
            const qint64 written = m_StreamProcess->write(m_SystemAuthTicket);
            // EOF is part of the one-shot FD protocol. It prevents a later
            // Moonlight component from reading further data from the pipe.
            m_StreamProcess->closeWriteChannel();
            if (written != expected) {
                setLastError(QStringLiteral("Could not provide the in-memory system-auth media ticket to Moonlight"));
                m_StreamProcess->kill();
                return;
            }
        }
        setRunning(true);
        emit streamBusyChanged();
        setStatus(QStringLiteral("Moonlight process started; wait until its stream window is visible"));
        emit streamStarted();
    });
    connect(m_StreamProcess, qOverload<int, QProcess::ExitStatus>(&QProcess::finished),
            this, &MoonlightController::finishStream);
    connect(m_StreamProcess, &QProcess::errorOccurred, this,
            [this](QProcess::ProcessError) {
                if (!m_Running) {
                    m_HasPendingRestart = false;
                    m_StopRequested = false;
                    ++m_RestartGeneration;
                    emit streamBusyChanged();
                    emit streamStoppingChanged();
                    setLastError(QStringLiteral("Unable to start Moonlight: %1")
                                     .arg(boundedText(m_StreamProcess->errorString())));
                }
            });

    m_PairProcess->setProcessChannelMode(QProcess::MergedChannels);
    connect(m_PairProcess, &QProcess::started, this, [this]() {
        emit pairProcessStarted();
    });
    connect(m_PairProcess, &QProcess::readyRead, this, [this]() {
        appendOutput(m_PairProcess->readAll());
    });
    connect(m_PairProcess, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this,
            [this](int exitCode, QProcess::ExitStatus exitStatus) {
                appendOutput(m_PairProcess->readAll());
                flushOutputFragment();
                const bool cancelled = m_PairCancelRequested;
                m_PairCancelRequested = false;
                setPairing(false);
                if (cancelled) {
                    setLastError(QString());
                    setStatus(QStringLiteral("Moonlight pairing cancelled"));
                }
                else if (exitStatus == QProcess::NormalExit && exitCode == 0) {
                    // Moonlight's CLI closes its error dialog with exit code
                    // zero too, so process status is not a pairing verdict.
                    setLastError(QString());
                    setStatus(QStringLiteral("Moonlight pairing flow exited; verify the host is paired before streaming"));
                }
                else {
                    setLastError(QStringLiteral("Moonlight pairing failed (exit %1)").arg(exitCode));
                    setStatus(QStringLiteral("Moonlight pairing failed"));
                }
            });
    connect(m_PairProcess, &QProcess::errorOccurred, this,
            [this](QProcess::ProcessError) {
                if (m_PairProcess->error() == QProcess::FailedToStart) {
                    m_PairCancelRequested = false;
                    setPairing(false);
                    setLastError(QStringLiteral("Unable to start Moonlight pairing: %1")
                                     .arg(boundedText(m_PairProcess->errorString())));
                }
            });

    m_StopTimer->setSingleShot(true);
    // Moonlight can need up to roughly thirty seconds to close its RTP and
    // decoder resources. Give it that graceful path before a last-resort kill.
    m_StopTimer->setInterval(35000);
    connect(m_StopTimer, &QTimer::timeout, this, [this]() {
        if (m_StreamProcess->state() != QProcess::NotRunning) {
            m_StreamProcess->kill();
        }
    });
}

MoonlightController::~MoonlightController()
{
    clearEphemeralSystemAuthCaFile();
}

QString MoonlightController::binaryPath() const
{
    return m_BinaryPath;
}

bool MoonlightController::running() const
{
    return m_Running;
}

bool MoonlightController::streamBusy() const
{
    return m_HasPendingRestart || m_StreamProcess->state() != QProcess::NotRunning;
}

bool MoonlightController::streamStopping() const
{
    return m_StopRequested;
}

bool MoonlightController::pairing() const
{
    return m_Pairing;
}

bool MoonlightController::canStartStream() const
{
    const bool hasCurrentMediaTicket = !m_SystemAuthGameStreamLeaseRequired ||
        (!m_SystemAuthTicket.isEmpty() &&
         m_SystemAuthTicketExpiresAtUtcMs > QDateTime::currentMSecsSinceEpoch());
    return m_SystemAuthAdmission && hasCurrentMediaTicket && !m_ProfileHandoffStartBlocked;
}

QString MoonlightController::status() const
{
    return m_Status;
}

QString MoonlightController::lastError() const
{
    return m_LastError;
}

QString MoonlightController::recentOutput() const
{
    return m_RecentOutput;
}

QStringList MoonlightController::profileIds() const
{
    return m_ProfileIds;
}

QString MoonlightController::currentProfileId() const
{
    return m_CurrentProfileId;
}

QString MoonlightController::profileProxmoxEndpoint() const
{
    return m_ProfileProxmoxEndpoint;
}

QString MoonlightController::profileVmId() const
{
    return m_ProfileVmId;
}

QString MoonlightController::profileHost() const
{
    return m_ProfileHost;
}

QString MoonlightController::profileAppName() const
{
    return m_ProfileAppName;
}

QString MoonlightController::profileResolution() const
{
    return m_ProfileResolution;
}

QString MoonlightController::profileDisplayMode() const
{
    return m_ProfileDisplayMode;
}

int MoonlightController::profileFps() const
{
    return m_ProfileFps;
}

int MoonlightController::profileBitrateKbps() const
{
    return m_ProfileBitrateKbps;
}

QString MoonlightController::profileVideoCodec() const
{
    return m_ProfileVideoCodec;
}

QString MoonlightController::videoDecoder() const
{
    return m_VideoDecoder;
}

#ifdef QSUNSHINE_TEST_MOONLIGHT_OVERRIDE
bool MoonlightController::setTestMoonlightBinary(const QString& binaryPath)
{
    if (profileHandoffBlocksConfigurationChange(QStringLiteral("executable configuration"))) {
        return false;
    }
    const QString normalized = binaryPath.trimmed();
    if (normalized.isEmpty() || !QFileInfo(normalized).isAbsolute()) {
        setLastError(QStringLiteral("Test Moonlight executable path must be absolute and non-empty"));
        return false;
    }
    if (normalized == m_BinaryPath) {
        return true;
    }
    if (streamBusy() || m_Pairing) {
        setLastError(QStringLiteral("Disconnect or cancel pairing before changing the test Moonlight executable"));
        return false;
    }
    m_BinaryPath = normalized;
    emit binaryPathChanged();
    return true;
}
#endif

void MoonlightController::setVideoDecoder(const QString& videoDecoder)
{
    if (profileHandoffBlocksConfigurationChange(QStringLiteral("decoder configuration"))) {
        return;
    }
    const QString normalized = videoDecoder.trimmed();
    if (!isSupportedVideoDecoder(normalized)) {
        setLastError(QStringLiteral("Moonlight video decoder must be auto, software, or hardware"));
        return;
    }
    if (normalized == m_VideoDecoder) {
        return;
    }
    if (streamBusy() || m_Pairing) {
        setLastError(QStringLiteral("Disconnect or cancel pairing before changing the Moonlight video decoder"));
        return;
    }
    m_VideoDecoder = normalized;
    QSettings settings;
    settings.setValue(QStringLiteral("q-sunshine/client/videoDecoder"), m_VideoDecoder);
    setLastError(QString());
    setStatus(QStringLiteral("Moonlight video decoder: %1").arg(m_VideoDecoder));
    emit videoDecoderChanged();
}

bool MoonlightController::validProfileId(const QString& profileId, QString* error)
{
    if (profileId.isEmpty() || profileId.size() > 64 || hasControlCharacter(profileId)) {
        if (error != nullptr) {
            *error = QStringLiteral("Profile name must contain 1 to 64 printable characters");
        }
        return false;
    }
    return true;
}

QString MoonlightController::profileSettingsGroup(const QString& profileId) const
{
    const QByteArray hash = QCryptographicHash::hash(
        profileId.normalized(QString::NormalizationForm_C).toUtf8(),
        QCryptographicHash::Sha256).toHex();
    return QStringLiteral("q-sunshine/client/profiles/") + QString::fromLatin1(hash);
}

void MoonlightController::loadProfile(const QString& profileId)
{
    QSettings settings;
    settings.beginGroup(profileSettingsGroup(profileId));
    m_ProfileProxmoxEndpoint = settings.value(QStringLiteral("proxmoxEndpoint")).toString().trimmed();
    m_ProfileVmId = settings.value(QStringLiteral("vmId")).toString().trimmed();
    m_ProfileHost = settings.value(QStringLiteral("host")).toString().trimmed();
    m_ProfileAppName = settings.value(QStringLiteral("appName"),
                                      kQemuConsoleApplication).toString().trimmed();
    m_ProfileResolution = settings.value(QStringLiteral("resolution"),
                                         QStringLiteral("1920x1080")).toString().trimmed();
    m_ProfileDisplayMode = normalizeDisplayMode(
        settings.value(QStringLiteral("displayMode"),
                       QStringLiteral("windowed")).toString().trimmed());
    m_ProfileFps = settings.value(QStringLiteral("fps"), 0).toInt();
    m_ProfileBitrateKbps = settings.value(QStringLiteral("bitrateKbps"), 0).toInt();
    m_ProfileVideoCodec = settings.value(QStringLiteral("videoCodec"),
                                         QStringLiteral("auto")).toString().trimmed();
    settings.endGroup();

    if (m_ProfileProxmoxEndpoint.size() > 255 || hasControlCharacter(m_ProfileProxmoxEndpoint)) {
        m_ProfileProxmoxEndpoint.clear();
    }
    if (!validVmId(m_ProfileVmId)) {
        m_ProfileVmId.clear();
    }
    if (m_ProfileAppName.isEmpty() || m_ProfileAppName.size() > 256 ||
        hasControlCharacter(m_ProfileAppName)) {
        m_ProfileAppName = kQemuConsoleApplication;
    }
    if (!isSupportedResolution(m_ProfileResolution)) {
        m_ProfileResolution = QStringLiteral("1920x1080");
    }
    if (!isSupportedDisplayMode(m_ProfileDisplayMode)) {
        m_ProfileDisplayMode = QStringLiteral("windowed");
    }
    if (m_ProfileFps != 0 && (m_ProfileFps < 10 || m_ProfileFps > 240)) {
        m_ProfileFps = 0;
    }
    if (m_ProfileBitrateKbps != 0 &&
        (m_ProfileBitrateKbps < 500 || m_ProfileBitrateKbps > 500000)) {
        m_ProfileBitrateKbps = 0;
    }
    if (!isSupportedStreamVideoCodec(m_ProfileVideoCodec)) {
        m_ProfileVideoCodec = QStringLiteral("auto");
    }
}

void MoonlightController::writeProfileIndex() const
{
    QSettings settings;
    settings.setValue(QStringLiteral("q-sunshine/client/profileIds"), m_ProfileIds);
    settings.setValue(QStringLiteral("q-sunshine/client/currentProfileId"), m_CurrentProfileId);
}

void MoonlightController::writeCurrentProfile() const
{
    QSettings settings;
    settings.beginGroup(profileSettingsGroup(m_CurrentProfileId));
    settings.setValue(QStringLiteral("profileId"), m_CurrentProfileId);
    settings.setValue(QStringLiteral("proxmoxEndpoint"), m_ProfileProxmoxEndpoint);
    settings.setValue(QStringLiteral("vmId"), m_ProfileVmId);
    settings.setValue(QStringLiteral("host"), m_ProfileHost);
    settings.setValue(QStringLiteral("appName"), m_ProfileAppName);
    settings.setValue(QStringLiteral("resolution"), m_ProfileResolution);
    settings.setValue(QStringLiteral("displayMode"), m_ProfileDisplayMode);
    settings.setValue(QStringLiteral("fps"), m_ProfileFps);
    settings.setValue(QStringLiteral("bitrateKbps"), m_ProfileBitrateKbps);
    settings.setValue(QStringLiteral("videoCodec"), m_ProfileVideoCodec);
    settings.endGroup();
}

bool MoonlightController::selectProfile(const QString& profileId)
{
    if (profileHandoffBlocksConfigurationChange(QStringLiteral("desktop profile selection"))) {
        return false;
    }
    const QString normalized = normalizedProfileId(profileId);
    QString error;
    if (!validProfileId(normalized, &error)) {
        setLastError(error);
        return false;
    }
    if (streamBusy() || m_Pairing) {
        setLastError(QStringLiteral("Disconnect or cancel pairing before changing desktop profiles"));
        return false;
    }
    if (!m_ProfileIds.contains(normalized)) {
        setLastError(QStringLiteral("Desktop profile does not exist: %1").arg(normalized));
        return false;
    }
    if (normalized == m_CurrentProfileId) {
        return true;
    }
    m_CurrentProfileId = normalized;
    loadProfile(m_CurrentProfileId);
    writeProfileIndex();
    setLastError(QString());
    setStatus(QStringLiteral("Desktop profile selected: %1").arg(m_CurrentProfileId));
    emit streamAuthorizationScopeChanged();
    emit profileChanged();
    return true;
}

bool MoonlightController::saveProfile(const QString& profileId, const QString& host,
                                      const QString& appName, const QString& resolution,
                                      const QString& displayMode)
{
    if (profileHandoffBlocksConfigurationChange(QStringLiteral("desktop profile changes"))) {
        return false;
    }
    const QString profileKey = normalizedProfileId(profileId);
    const StreamRequest request {host.trimmed(), appName.trimmed(), resolution.trimmed(),
                                 displayMode.trimmed(), m_ProfileFps,
                                 m_ProfileBitrateKbps, m_ProfileVideoCodec};
    QString error;
    if (!validProfileId(profileKey, &error)) {
        setLastError(error);
        return false;
    }
    if (m_Pairing ||
        (streamBusy() && (profileKey != m_CurrentProfileId ||
                          request.host != m_ProfileHost))) {
        setLastError(QStringLiteral("Disconnect before creating or switching desktop profiles"));
        return false;
    }
    if (request.host.isEmpty() || request.host.size() > 255 || hasControlCharacter(request.host)) {
        setLastError(QStringLiteral("Provide a valid Sunshine host name, address, or UUID"));
        return false;
    }
    if (request.appName.isEmpty() || request.appName.size() > 256 ||
        hasControlCharacter(request.appName)) {
        setLastError(QStringLiteral("Provide a valid Sunshine application name"));
        return false;
    }
    if (!isSupportedDisplayMode(request.displayMode)) {
        setLastError(QStringLiteral("Display mode must be windowed or fullscreen"));
        return false;
    }
    if (!isSupportedResolution(request.resolution) ||
        (request.fps != 0 && (request.fps < 10 || request.fps > 240)) ||
        (request.bitrateKbps != 0 &&
         (request.bitrateKbps < 500 || request.bitrateKbps > 500000)) ||
        !isSupportedStreamVideoCodec(request.videoCodec)) {
        setLastError(QStringLiteral("Provide a supported resolution and presentation mode"));
        return false;
    }

    const QString previousProfileId = m_CurrentProfileId;
    const QString previousHost = m_ProfileHost;
    const bool isNewProfile = !m_ProfileIds.contains(profileKey);
    if (isNewProfile) {
        m_ProfileIds.append(profileKey);
    }
    m_CurrentProfileId = profileKey;
    m_ProfileHost = request.host;
    m_ProfileAppName = request.appName;
    m_ProfileResolution = request.resolution;
    m_ProfileDisplayMode = request.displayMode;
    m_ProfileFps = request.fps;
    m_ProfileBitrateKbps = request.bitrateKbps;
    m_ProfileVideoCodec = request.videoCodec;
    writeCurrentProfile();
    writeProfileIndex();
    setLastError(QString());
    setStatus(QStringLiteral("Desktop profile saved: %1").arg(m_CurrentProfileId));
    if (isNewProfile) {
        emit profilesChanged();
    }
    // Do this at the controller boundary, not just in Main.qml. Any direct
    // in-process caller that changes VM profile or Sunshine host must lose a
    // system-auth admission issued for the previous route before it can ask
    // the controller to start a stream.
    if (previousProfileId != m_CurrentProfileId || previousHost != m_ProfileHost) {
        emit streamAuthorizationScopeChanged();
    }
    emit profileChanged();
    return true;
}

bool MoonlightController::validVmId(const QString& vmId)
{
    // Proxmox VMIDs are positive decimal integers.  Keep the upper bound
    // inside a signed 32-bit range so this metadata can later be represented
    // exactly in protocol JSON without a float round-trip.
    static const QRegularExpression pattern(QStringLiteral("\\A[1-9][0-9]{0,8}\\z"));
    return pattern.match(vmId).hasMatch();
}

bool MoonlightController::saveConnectionIdentity(const QString& proxmoxEndpoint,
                                                  const QString& vmId)
{
    if (profileHandoffBlocksConfigurationChange(QStringLiteral("connection identity changes"))) {
        return false;
    }
    const QString endpoint = proxmoxEndpoint.trimmed();
    const QString normalizedVmId = vmId.trimmed();
    if (endpoint.isEmpty() || endpoint.size() > 255 || hasControlCharacter(endpoint)) {
        setLastError(QStringLiteral("Provide a valid Proxmox host name or address"));
        return false;
    }
    if (!validVmId(normalizedVmId)) {
        setLastError(QStringLiteral("Proxmox VMID must be a positive decimal integer"));
        return false;
    }
    if (m_ProfileProxmoxEndpoint == endpoint && m_ProfileVmId == normalizedVmId) {
        return true;
    }
    m_ProfileProxmoxEndpoint = endpoint;
    m_ProfileVmId = normalizedVmId;
    writeCurrentProfile();
    setLastError(QString());
    setStatus(QStringLiteral("Connection identity saved for VM %1").arg(m_ProfileVmId));
    emit profileChanged();
    return true;
}

bool MoonlightController::validateExecutable(QString* error) const
{
#ifndef QSUNSHINE_TEST_MOONLIGHT_OVERRIDE
    const QString trustedPath = packagedMoonlightBinary();
    if (m_BinaryPath != trustedPath) {
        *error = QStringLiteral("Moonlight executable is not the trusted packaged child");
        return false;
    }
#endif
    const QFileInfo binary(m_BinaryPath);
    if (!binary.isFile() || !binary.isExecutable()) {
        *error = QStringLiteral("Trusted packaged Moonlight executable is unavailable: %1")
                     .arg(m_BinaryPath);
        return false;
    }
    return true;
}

bool MoonlightController::validateStreamRequest(const StreamRequest& request, QString* error) const
{
    if (request.host.isEmpty() || request.host.size() > 255 || hasControlCharacter(request.host)) {
        *error = QStringLiteral("Provide a valid Moonlight host name, address, or UUID");
        return false;
    }
    if (request.appName.isEmpty() || request.appName.size() > 256 || hasControlCharacter(request.appName)) {
        *error = QStringLiteral("Provide a valid Sunshine application name");
        return false;
    }
    if (!isSupportedResolution(request.resolution)) {
        *error = QStringLiteral("Resolution must have WIDTHxHEIGHT form");
        return false;
    }
    if (!isSupportedDisplayMode(request.displayMode)) {
        *error = QStringLiteral("Display mode must be windowed or fullscreen");
        return false;
    }
    if (request.fps != 0 && (request.fps < 10 || request.fps > 240)) {
        *error = QStringLiteral("Stream FPS must be automatic or within 10..240");
        return false;
    }
    if (request.bitrateKbps != 0 &&
        (request.bitrateKbps < 500 || request.bitrateKbps > 500000)) {
        *error = QStringLiteral("Stream bitrate must be automatic or within 500..500000 Kbps");
        return false;
    }
    if (!isSupportedStreamVideoCodec(request.videoCodec)) {
        *error = QStringLiteral("Stream video codec must be auto, H.264, HEVC, or AV1");
        return false;
    }
    return validateExecutable(error);
}

void MoonlightController::pair(const QString& host, const QString& pin)
{
    if (profileHandoffBlocksConfigurationChange(QStringLiteral("pairing"))) {
        return;
    }
    if (m_SystemAuthGameStreamLeaseRequired) {
        // Production composition selects this mode before QML is loaded.
        // Refuse even an accidental in-process caller: native Sunshine has no
        // pairing endpoint and must never regain a PIN fallback.
        setLastError(QStringLiteral("PIN pairing is disabled for system-authenticated GameStream"));
        return;
    }
    if (!m_SystemAuthAdmission) {
        setStatus(QStringLiteral("System authentication is required before a legacy pairing compatibility action"));
        return;
    }
    QString error;
    if (streamBusy()) {
        setLastError(QStringLiteral("Stop the active stream before starting pairing"));
        return;
    }
    if (m_Pairing || m_PairProcess->state() != QProcess::NotRunning) {
        setLastError(QStringLiteral("A Moonlight pairing operation is already running"));
        return;
    }
    if (host.trimmed().isEmpty() || host.trimmed().size() > 255 || hasControlCharacter(host)) {
        setLastError(QStringLiteral("Provide a valid Moonlight host for pairing"));
        return;
    }
    if (!QRegularExpression(QStringLiteral("\\A[0-9]{4}\\z")).match(pin).hasMatch()) {
        setLastError(QStringLiteral("Moonlight pairing PIN must contain exactly four digits"));
        return;
    }
    if (!validateExecutable(&error)) {
        setLastError(error);
        return;
    }

    setLastError(QString());
    setStatus(QStringLiteral("Starting Moonlight pairing"));
    m_PairCancelRequested = false;
    ++m_PairGeneration;
    setPairing(true);
    m_PairProcess->start(m_BinaryPath,
                         {QStringLiteral("pair"), QStringLiteral("--pin"), pin,
                          // Keep user-provided host data from being parsed as
                          // a Moonlight CLI option.
                          QStringLiteral("--"), host.trimmed()});
}

void MoonlightController::cancelPairing()
{
    if (!m_Pairing || m_PairProcess->state() == QProcess::NotRunning) {
        return;
    }
    m_PairCancelRequested = true;
    setStatus(QStringLiteral("Cancelling Moonlight pairing"));
    m_PairProcess->terminate();
    const quint64 pairingGeneration = m_PairGeneration;
    QTimer::singleShot(3000, this, [this, pairingGeneration]() {
        // A previous cancellation timeout must never kill a later pairing
        // operation that reused the single QProcess instance.
        if (m_Pairing && m_PairGeneration == pairingGeneration &&
            m_PairProcess->state() != QProcess::NotRunning) {
            m_PairProcess->kill();
        }
    });
}

void MoonlightController::startStream(const QString& host, const QString& appName,
                                      const QString& resolution, const QString& displayMode)
{
    if (m_ProfileHandoffStartBlocked) {
        // This is an expected admission refusal, not a Moonlight process
        // failure.  In particular, ProfileNegotiationCoordinator observes
        // lastErrorChanged() as a real handoff fault, so keep this feedback
        // on the non-fatal status channel and leave the remote request alive.
        setStatus(QStringLiteral("The display-profile handoff owns Moonlight start until the guest transaction is terminal"));
        return;
    }
    if (!m_SystemAuthAdmission) {
        setStatus(m_SystemAuthGameStreamLeaseRequired
                      ? QStringLiteral("Sign in with a system account before starting PIN-free GameStream")
                      : QStringLiteral("Sign in with a system account before starting the legacy GameStream compatibility path"));
        return;
    }
    const QString requestedProfileHost = host.trimmed();
    // A terminal broker route is C++-installed only after authenticated,
    // schema-validated route discovery. Preserve the public API's profile
    // scope check below, but never let its user-facing endpoint replace the
    // broker-authoritative media destination.
    const QString streamHost = m_SystemAuthMediaHost.isEmpty()
        ? requestedProfileHost : m_SystemAuthMediaHost;
    // A PVE-issued route intentionally has no caller-selectable Sunshine
    // application.  The transport endpoint is the VM console itself.  This
    // also makes an old persisted "Desktop" setting inert before it can be
    // put on Moonlight's command line.
    const QString streamApplication = m_SystemAuthMediaHost.isEmpty()
        ? appName.trimmed() : kQemuConsoleApplication;
    StreamRequest request {streamHost, streamApplication, resolution.trimmed(), displayMode.trimmed(),
                           m_ProfileFps, m_ProfileBitrateKbps, m_ProfileVideoCodec};
    QString error;
    if (!validateStreamRequest(request, &error)) {
        setLastError(error);
        return;
    }
    // The public launch API operates on the selected saved desktop route.
    // Requiring the caller to save a changed host first makes the controller's
    // scope-change signal (and therefore system-auth revocation) unavoidable.
    if (requestedProfileHost != m_ProfileHost) {
        setLastError(QStringLiteral("Save the requested Sunshine host in the selected desktop profile before connecting"));
        return;
    }
    if (m_Pairing || m_PairProcess->state() != QProcess::NotRunning) {
        setLastError(QStringLiteral("Wait for Moonlight pairing to finish before connecting"));
        return;
    }
    startValidatedStreamRequest(request, m_SystemAuthMediaHost.isEmpty());
}

bool MoonlightController::applyNegotiatedProfile(int width, int height, int fps,
                                                  int bitrateKbps, const QString& videoCodec)
{
    if (m_ProfileHandoffStartBlocked) {
        setStatus(QStringLiteral("The display-profile handoff owns Moonlight start until the guest transaction is terminal"));
        return false;
    }
    return applyNegotiatedProfileForHandoff(width, height, fps, bitrateKbps, videoCodec);
}

bool MoonlightController::applyNegotiatedProfileForHandoff(int width, int height, int fps,
                                                            int bitrateKbps,
                                                            const QString& videoCodec)
{
    const QString resolution = QStringLiteral("%1x%2").arg(width).arg(height);
    const QString streamHost = m_SystemAuthMediaHost.isEmpty()
        ? m_ProfileHost : m_SystemAuthMediaHost;
    const QString streamApplication = m_SystemAuthMediaHost.isEmpty()
        ? m_ProfileAppName : kQemuConsoleApplication;
    StreamRequest request {streamHost, streamApplication, resolution, m_ProfileDisplayMode,
                           fps, bitrateKbps, videoCodec};
    QString error;
    // A connection profile owns QEMU SetUIInfo and the guest scanout.  It is
    // therefore valid only after the coordinator has retired the old
    // Moonlight/Sunshine capture.  Restarting an active stream here would
    // leave its old capture free to overwrite the new scanout geometry.
    if (m_Running || m_StopRequested || m_HasPendingRestart ||
        m_StreamProcess->state() != QProcess::NotRunning) {
        setLastError(QStringLiteral("Wait for the current Moonlight stream to stop before launching a negotiated profile"));
        return false;
    }
    if (!m_SystemAuthAdmission) {
        setLastError(QStringLiteral("System authentication is required before launching a negotiated GameStream profile"));
        return false;
    }
    if (m_Pairing || m_PairProcess->state() != QProcess::NotRunning) {
        setLastError(QStringLiteral("Wait for Moonlight pairing to finish before applying a negotiated profile"));
        return false;
    }
    if (!validateStreamRequest(request, &error)) {
        setLastError(error);
        return false;
    }
    startValidatedStreamRequest(request, m_SystemAuthMediaHost.isEmpty());
    return true;
}

void MoonlightController::setProfileHandoffStartBlocked(bool blocked)
{
    const bool previousCanStart = canStartStream();
    m_ProfileHandoffStartBlocked = blocked;
    if (previousCanStart != canStartStream()) {
        emit canStartStreamChanged();
    }
}

void MoonlightController::setSystemAuthAdmission(bool admitted)
{
    if (m_SystemAuthAdmission == admitted) {
        return;
    }
    m_SystemAuthAdmission = admitted;
    emit canStartStreamChanged();
    if (admitted) {
        setStatus(m_SystemAuthGameStreamLeaseRequired
                      ? QStringLiteral("System authentication admits a PIN-free GameStream launch")
                      : QStringLiteral("System authentication admits a legacy GameStream compatibility launch"));
        return;
    }

    // This is an admission gate, not a media kill switch. A short-lived
    // system-auth ticket is consumed to obtain Sunshine's separate mTLS
    // media lease; expiry must forbid a new launch and erase the ticket while
    // allowing an already admitted RTP session to finish. The composition
    // root explicitly calls stopStream() for a user logout or a route change.
    setStatus(QStringLiteral("System authentication is required before starting a stream"));
}

void MoonlightController::requireSystemAuthGameStreamLease()
{
    if (m_SystemAuthGameStreamLeaseRequired) {
        return;
    }
    const bool previousCanStart = canStartStream();
    m_SystemAuthGameStreamLeaseRequired = true;
    if (previousCanStart != canStartStream()) {
        emit canStartStreamChanged();
    }
}

void MoonlightController::setSystemAuthGameStreamLease(const QString& authHost, int authPort,
                                                        const QString& authServerName,
                                                        const QString& authCaFile,
                                                        const QString& audience,
                                                        const QByteArray& ticket,
                                                        qint64 expiresAtUtcMs)
{
    requireSystemAuthGameStreamLease();
    const bool previousCanStart = canStartStream();
    m_SystemAuthTicket.fill('\0');
    m_SystemAuthTicket.clear();
    m_SystemAuthHost = authHost.trimmed();
    m_SystemAuthPort = authPort;
    m_SystemAuthServerName = authServerName.trimmed();
    m_SystemAuthCaFile = authCaFile.trimmed();
    m_SystemAuthAudience = audience.trimmed();
    m_SystemAuthTicket = ticket;
    m_SystemAuthTicketExpiresAtUtcMs = expiresAtUtcMs;
    if (previousCanStart != canStartStream()) {
        emit canStartStreamChanged();
    }
}

bool MoonlightController::setSystemAuthGameStreamLeasePem(const QString& authHost, int authPort,
                                                           const QString& authServerName,
                                                           const QByteArray& authCaPem,
                                                           const QString& audience,
                                                           const QByteArray& ticket,
                                                           qint64 expiresAtUtcMs)
{
    if (authCaPem.isEmpty() || authCaPem.size() > kMaxSystemAuthCaBytes ||
        authCaPem.contains("PRIVATE KEY")) {
        setLastError(QStringLiteral("Broker returned invalid transport trust material"));
        return false;
    }
    if (streamBusy() && m_SystemAuthEphemeralCaFile) {
        // Replacing a launch route underneath a live child could remove the
        // exact CA file it is still using. A new descriptor must wait for
        // normal stream teardown instead.
        setLastError(QStringLiteral("Wait for the current Moonlight stream before replacing launch trust"));
        return false;
    }
    clearEphemeralSystemAuthCaFile();
    auto file = std::make_unique<QTemporaryFile>(
        QDir::tempPath() + QStringLiteral("/q-sunshine-moonlight-ca-XXXXXX.pem"));
    file->setAutoRemove(true);
    if (!file->open() ||
        !file->setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner) ||
        file->write(authCaPem) != authCaPem.size() || !file->flush()) {
        if (file->isOpen()) {
            file->close();
        }
        file->remove();
        setLastError(QStringLiteral("Could not create ephemeral Moonlight trust file"));
        return false;
    }
    const QString filePath = file->fileName();
    file->close();
    const QFileInfo info(filePath);
    const QFileDevice::Permissions permissions = info.permissions();
    if (!info.isFile() || (permissions & (QFileDevice::ReadGroup | QFileDevice::WriteGroup |
                                          QFileDevice::ExeGroup | QFileDevice::ReadOther |
                                          QFileDevice::WriteOther | QFileDevice::ExeOther))) {
        file->remove();
        setLastError(QStringLiteral("Ephemeral Moonlight trust file permissions are unsafe"));
        return false;
    }
    m_SystemAuthEphemeralCaFile = std::move(file);
    m_RemoveEphemeralSystemAuthCaWhenStopped = false;
    setSystemAuthGameStreamLease(authHost, authPort, authServerName, filePath, audience,
                                 ticket, expiresAtUtcMs);
    return true;
}

bool MoonlightController::setSystemAuthGameStreamMediaRoute(const QString& host, int basePort)
{
    const QString normalizedHost = host.trimmed();
    if (normalizedHost.isEmpty() || normalizedHost.size() > 253 || basePort <= 5 ||
        basePort > 65535 || hasControlCharacter(normalizedHost) ||
        normalizedHost.contains(QLatin1Char('/')) || normalizedHost.contains(QLatin1Char('@')) ||
        normalizedHost.contains(QLatin1Char('?')) || normalizedHost.contains(QLatin1Char('#'))) {
        setLastError(QStringLiteral("Terminal-server media route is invalid"));
        return false;
    }
    QString endpointHost = normalizedHost;
    if (normalizedHost.contains(QLatin1Char(':'))) {
        // SystemAuthClient validates broker route hosts with QHostAddress
        // before this C++ composition hook is reached. Keep this
        // QtCore-only controller free of a QtNetwork dependency while
        // formatting the already-validated bare IPv6 literal for Moonlight.
        endpointHost = QStringLiteral("[") + normalizedHost + QStringLiteral("]");
    }
    m_SystemAuthMediaHost = endpointHost + QLatin1Char(':') + QString::number(basePort);
    return true;
}

void MoonlightController::clearSystemAuthGameStreamLease()
{
    const bool previousCanStart = canStartStream();
    m_SystemAuthTicket.fill('\0');
    m_SystemAuthTicket.clear();
    m_SystemAuthHost.clear();
    m_SystemAuthPort = 0;
    m_SystemAuthServerName.clear();
    m_SystemAuthCaFile.clear();
    m_SystemAuthAudience.clear();
    m_SystemAuthTicketExpiresAtUtcMs = 0;
    m_SystemAuthMediaHost.clear();
    if (streamBusy()) {
        m_RemoveEphemeralSystemAuthCaWhenStopped = true;
    }
    else {
        clearEphemeralSystemAuthCaFile();
    }
    if (previousCanStart != canStartStream()) {
        emit canStartStreamChanged();
    }
}

void MoonlightController::clearEphemeralSystemAuthCaFile()
{
    if (!m_SystemAuthEphemeralCaFile) {
        return;
    }
    QTemporaryFile* file = m_SystemAuthEphemeralCaFile.get();
    QFile scrub(file->fileName());
    if (scrub.open(QIODevice::ReadWrite)) {
        const qint64 size = scrub.size();
        if (size > 0 && scrub.seek(0)) {
            QByteArray zeros(static_cast<int>(qMin<qint64>(size, 4096)), '\0');
            qint64 remaining = size;
            while (remaining > 0) {
                const qint64 written = scrub.write(zeros.constData(), qMin<qint64>(remaining, zeros.size()));
                if (written <= 0) {
                    break;
                }
                remaining -= written;
            }
            zeros.fill('\0');
            scrub.flush();
        }
        scrub.resize(0);
        scrub.close();
    }
    file->remove();
    m_SystemAuthEphemeralCaFile.reset();
    m_RemoveEphemeralSystemAuthCaWhenStopped = false;
}

bool MoonlightController::profileHandoffBlocksConfigurationChange(const QString& operation)
{
    if (!m_ProfileHandoffStartBlocked) {
        return false;
    }

    // Do not use lastError here: ProfileNegotiationCoordinator observes that
    // signal as a genuine remote-handoff failure.  This is an expected public
    // API admission refusal while it owns the launch configuration.
    setStatus(QStringLiteral("The display-profile handoff owns Moonlight %1 until the guest transaction is terminal")
                  .arg(operation));
    return true;
}

void MoonlightController::startValidatedStreamRequest(const StreamRequest& request,
                                                       bool persistProfile)
{
    const quint64 requestGeneration = ++m_RestartGeneration;
    if (persistProfile) {
        m_ProfileHost = request.host;
    }
    m_ProfileAppName = request.appName;
    m_ProfileResolution = request.resolution;
    m_ProfileDisplayMode = request.displayMode;
    m_ProfileFps = request.fps;
    m_ProfileBitrateKbps = request.bitrateKbps;
    m_ProfileVideoCodec = request.videoCodec;
    if (persistProfile) {
        writeCurrentProfile();
        writeProfileIndex();
    }
    emit profileChanged();
    if (m_StreamProcess->state() != QProcess::NotRunning) {
        m_PendingRestart = request;
        m_HasPendingRestart = true;
        m_PendingRestartGeneration = requestGeneration;
        m_StopRequested = true;
        emit streamStoppingChanged();
        emit streamTeardownStarted();
        setStatus(QStringLiteral("Restarting Moonlight stream with new display settings"));
        m_StreamProcess->terminate();
        m_StopTimer->start();
        return;
    }
    launchStream(request);
}

bool MoonlightController::prepareSystemAuthGameStreamEnvironment(
    const StreamRequest& request, QProcessEnvironment* environment, QString* error) const
{
    if (environment == nullptr || error == nullptr) {
        return false;
    }
    static const QStringList leaseEnvironmentNames {
        QStringLiteral("QSM_GAMESTREAM_AUTH_HOST"),
        QStringLiteral("QSM_GAMESTREAM_AUTH_PORT"),
        QStringLiteral("QSM_GAMESTREAM_AUTH_SNI"),
        QStringLiteral("QSM_GAMESTREAM_AUTH_CA_FILE"),
        QStringLiteral("QSM_GAMESTREAM_AUDIENCE"),
        QStringLiteral("QSM_GAMESTREAM_TICKET_FD"),
        QStringLiteral("QSM_GAMESTREAM_HOST"),
        QStringLiteral("QSM_GAMESTREAM_HTTPS_PORT"),
    };
    // Never inherit a caller's partial or stale lease variables. They would
    // make an ordinary compatibility launch unexpectedly enter native mode.
    for (const QString& name : leaseEnvironmentNames) {
        environment->remove(name);
    }
    if (!m_SystemAuthGameStreamLeaseRequired) {
        return true;
    }
    if (m_SystemAuthTicket.isEmpty() ||
        m_SystemAuthTicketExpiresAtUtcMs <= QDateTime::currentMSecsSinceEpoch() ||
        m_SystemAuthHost.isEmpty() || m_SystemAuthPort < 1 || m_SystemAuthPort > 65535 ||
        m_SystemAuthCaFile.isEmpty() || m_SystemAuthAudience.isEmpty()) {
        *error = QStringLiteral("A current system-auth media lease is required before starting Moonlight");
        return false;
    }

    // Moonlight treats a manually supplied port as the GameStream HTTP base
    // port. Sunshine maps HTTPS at the stable -5 offset (47989 -> 47984), so
    // pass the exact HTTPS endpoint to the lease-aware NvHTTP path rather than
    // asking it to trust an unauthenticated /serverinfo discovery response.
    const QUrl endpoint = QUrl::fromUserInput(QStringLiteral("moonlight://") + request.host);
    const QString gameStreamHost = endpoint.host();
    const int httpBasePort = endpoint.port(47989);
    if (!endpoint.isValid() || gameStreamHost.isEmpty() || httpBasePort <= 5 ||
        httpBasePort > 65535) {
        *error = QStringLiteral("PIN-free GameStream requires a concrete Sunshine host and valid base port");
        return false;
    }
    const int httpsPort = httpBasePort - 5;
    const QString authSni = m_SystemAuthServerName.isEmpty() ? m_SystemAuthHost
                                                               : m_SystemAuthServerName;
    environment->insert(QStringLiteral("QSM_GAMESTREAM_AUTH_HOST"), m_SystemAuthHost);
    environment->insert(QStringLiteral("QSM_GAMESTREAM_AUTH_PORT"), QString::number(m_SystemAuthPort));
    environment->insert(QStringLiteral("QSM_GAMESTREAM_AUTH_SNI"), authSni);
    environment->insert(QStringLiteral("QSM_GAMESTREAM_AUTH_CA_FILE"), m_SystemAuthCaFile);
    environment->insert(QStringLiteral("QSM_GAMESTREAM_AUDIENCE"), m_SystemAuthAudience);
    environment->insert(QStringLiteral("QSM_GAMESTREAM_TICKET_FD"), QStringLiteral("0"));
    environment->insert(QStringLiteral("QSM_GAMESTREAM_HOST"), gameStreamHost);
    environment->insert(QStringLiteral("QSM_GAMESTREAM_HTTPS_PORT"), QString::number(httpsPort));
    return true;
}

void MoonlightController::launchStream(const StreamRequest& request)
{
    m_HasPendingRestart = false;
    m_StopRequested = false;
    emit streamStoppingChanged();
    m_RecentOutput.clear();
    m_OutputFragment.clear();
    m_DiscardingOutputLine = false;
    emit recentOutputChanged();
    setLastError(QString());
    setStatus(QStringLiteral("Starting Moonlight stream"));
    QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
    QString environmentError;
    if (!prepareSystemAuthGameStreamEnvironment(request, &environment, &environmentError)) {
        setLastError(environmentError);
        setStatus(QStringLiteral("Moonlight did not start because the system-auth media lease is unavailable"));
        return;
    }
    m_StreamProcess->setProcessEnvironment(environment);
    QStringList arguments {QStringLiteral("stream")};
    if (m_SystemAuthGameStreamLeaseRequired) {
        // Stock Moonlight does not know this switch and exits. It is a
        // deliberate fail-closed marker that prevents an accidental paired
        // certificate/PIN fallback when the child is not our patched build.
        arguments << QStringLiteral("--qsm-system-auth");
    }
    arguments << QStringLiteral("--display-mode") << request.displayMode
              << QStringLiteral("--resolution") << request.resolution
              // A desktop session is never an application that this shell is
              // allowed to terminate on disconnect.
              << QStringLiteral("--no-quit-after")
              << QStringLiteral("--absolute-mouse");
    // The default preserves stock Moonlight's automatic selection.  The two
    // explicit values are constrained enum choices, not arbitrary CLI text.
    if (m_VideoDecoder != QStringLiteral("auto")) {
        arguments << QStringLiteral("--video-decoder") << m_VideoDecoder;
    }
    if (request.fps != 0) {
        arguments << QStringLiteral("--fps") << QString::number(request.fps);
    }
    if (request.bitrateKbps != 0) {
        arguments << QStringLiteral("--bitrate") << QString::number(request.bitrateKbps);
    }
    if (request.videoCodec != QStringLiteral("auto")) {
        arguments << QStringLiteral("--video-codec") << request.videoCodec;
    }
    // Host and app are positional data, not an extension point for Moonlight
    // CLI options.
    arguments << QStringLiteral("--") << request.host << request.appName;
    m_StreamProcess->start(m_BinaryPath, arguments);
    emit streamBusyChanged();
}

void MoonlightController::stopStream()
{
    ++m_RestartGeneration;
    m_HasPendingRestart = false;
    emit streamBusyChanged();
    if (m_StreamProcess->state() == QProcess::NotRunning) {
        setStatus(QStringLiteral("Moonlight stream is already stopped"));
        return;
    }
    m_StopRequested = true;
    emit streamStoppingChanged();
    emit streamTeardownStarted();
    setStatus(QStringLiteral("Stopping Moonlight stream"));
    m_StreamProcess->terminate();
    m_StopTimer->start();
}

void MoonlightController::finishStream(int exitCode, QProcess::ExitStatus exitStatus)
{
    appendOutput(m_StreamProcess->readAll());
    flushOutputFragment();
    m_StopTimer->stop();
    const bool restartRequested = m_HasPendingRestart;
    const quint64 restartGeneration = m_PendingRestartGeneration;
    const bool intentionalStop = m_StopRequested || restartRequested;
    m_StopRequested = false;
    emit streamStoppingChanged();
    const bool wasRunning = m_Running;
    setRunning(false);
    if (m_RemoveEphemeralSystemAuthCaWhenStopped && !restartRequested) {
        clearEphemeralSystemAuthCaFile();
    }
    if (intentionalStop) {
        setStatus(QStringLiteral("Moonlight stream stopped"));
    }
    else if (exitStatus == QProcess::NormalExit && exitCode == 0) {
        // Stock Moonlight also returns zero after certain GUI connection
        // errors. Its process exit alone is not proof that a stream came up.
        setLastError(QStringLiteral("Moonlight exited without a reliable connection result; inspect diagnostics and verify the stream"));
        setStatus(QStringLiteral("Moonlight UI exited; verify whether the stream connected"));
    }
    else {
        setLastError(QStringLiteral("Moonlight stream ended unexpectedly (exit %1)").arg(exitCode));
        setStatus(QStringLiteral("Moonlight stream ended"));
    }
    if (wasRunning) {
        emit streamFinished();
    }
    emit streamBusyChanged();
    if (restartRequested) {
        const StreamRequest restart = m_PendingRestart;
        // Let Sunshine retire the previous RTP/RTSP session before its next
        // client connects.  This is short enough to feel like a controlled
        // reconnect but avoids racing a single-session host during a change
        // of presentation or initial stream resolution.
        QTimer::singleShot(kReconnectGracePeriodMs, this, [this, restart, restartGeneration]() {
            if (!m_HasPendingRestart || m_PendingRestartGeneration != restartGeneration ||
                m_RestartGeneration != restartGeneration ||
                m_StreamProcess->state() != QProcess::NotRunning) {
                return;
            }
            m_HasPendingRestart = false;
            emit streamBusyChanged();
            launchStream(restart);
        });
    }
}

void MoonlightController::appendOutput(const QByteArray& output)
{
    QByteArray remaining = output;
    if (m_DiscardingOutputLine) {
        const int newline = remaining.indexOf('\n');
        if (newline < 0) {
            return;
        }
        remaining.remove(0, newline + 1);
        m_DiscardingOutputLine = false;
    }

    while (!remaining.isEmpty()) {
        const int newline = remaining.indexOf('\n');
        if (newline < 0) {
            if (m_OutputFragment.size() + remaining.size() > kMaxOutputLineBytes) {
                // Never display a suffix from an unbounded line: it may be a
                // split credential whose identifying prefix was discarded.
                m_OutputFragment.clear();
                m_DiscardingOutputLine = true;
            }
            else {
                m_OutputFragment.append(remaining);
            }
            return;
        }

        const QByteArray line = remaining.left(newline + 1);
        remaining.remove(0, newline + 1);
        if (m_OutputFragment.size() + line.size() > kMaxOutputLineBytes) {
            m_OutputFragment.clear();
            continue;
        }
        m_OutputFragment.append(line);
        appendRedactedOutput(m_OutputFragment);
        m_OutputFragment.clear();
    }
}

void MoonlightController::appendRedactedOutput(const QByteArray& output)
{
    const QString decoded = redactMoonlightOutput(QString::fromLocal8Bit(output));
    if (decoded.isEmpty()) {
        return;
    }
    m_RecentOutput += decoded;
    if (m_RecentOutput.size() > kMaxOutputCharacters) {
        m_RecentOutput.remove(0, m_RecentOutput.size() - kMaxOutputCharacters);
    }
    emit recentOutputChanged();
}

void MoonlightController::flushOutputFragment()
{
    if (m_DiscardingOutputLine) {
        m_OutputFragment.clear();
        m_DiscardingOutputLine = false;
        return;
    }
    if (!m_OutputFragment.isEmpty()) {
        appendRedactedOutput(m_OutputFragment);
        m_OutputFragment.clear();
    }
}

void MoonlightController::setStatus(const QString& status)
{
    const QString normalized = boundedText(status);
    if (m_Status != normalized) {
        m_Status = normalized;
        emit statusChanged();
    }
}

void MoonlightController::setLastError(const QString& error)
{
    const QString normalized = boundedText(error);
    if (m_LastError != normalized) {
        m_LastError = normalized;
        emit lastErrorChanged();
    }
}

void MoonlightController::setRunning(bool running)
{
    if (m_Running != running) {
        m_Running = running;
        emit runningChanged();
    }
}

void MoonlightController::setPairing(bool pairing)
{
    if (m_Pairing != pairing) {
        m_Pairing = pairing;
        emit pairingChanged();
    }
}
