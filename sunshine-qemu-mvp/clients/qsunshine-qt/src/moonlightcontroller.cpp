// SPDX-License-Identifier: GPL-3.0-or-later

#include "moonlightcontroller.h"

#include <QCryptographicHash>
#include <QFileInfo>
#include <QRegularExpression>
#include <QSettings>
#include <QStandardPaths>
#include <QTimer>

namespace {

constexpr int kMaxOutputCharacters = 8192;
constexpr int kMaxOutputLineBytes = 64 * 1024;
constexpr int kReconnectGracePeriodMs = 500;

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
    return value == QStringLiteral("fullscreen") || value == QStringLiteral("windowed") ||
           value == QStringLiteral("borderless");
}

bool isSupportedVideoDecoder(const QString& value)
{
    return value == QStringLiteral("auto") || value == QStringLiteral("software") ||
           value == QStringLiteral("hardware");
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
    output.replace(querySecret, QStringLiteral("\\1[redacted]"));
    output.replace(headerSecret, QStringLiteral("\\1 [redacted]"));
    output.replace(pinSecret, QStringLiteral("\\1[redacted]"));
    return output;
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
      m_PairCancelRequested(false),
      m_PairGeneration(0),
      m_RestartGeneration(0),
      m_PendingRestartGeneration(0)
{
    QSettings settings;
    m_BinaryPath = settings.value(QStringLiteral("q-sunshine/client/moonlightBinary")).toString().trimmed();
    if (m_BinaryPath.isEmpty()) {
        m_BinaryPath = QStandardPaths::findExecutable(QStringLiteral("moonlight"));
        if (m_BinaryPath.isEmpty()) {
            m_BinaryPath = QStringLiteral("moonlight");
        }
    }
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
    connect(m_StreamProcess, &QProcess::readyRead, this, [this]() {
        appendOutput(m_StreamProcess->readAll());
    });
    connect(m_StreamProcess, &QProcess::started, this, [this]() {
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

QString MoonlightController::videoDecoder() const
{
    return m_VideoDecoder;
}

void MoonlightController::setBinaryPath(const QString& binaryPath)
{
    const QString normalized = binaryPath.trimmed();
    if (normalized.isEmpty()) {
        setLastError(QStringLiteral("Moonlight executable path must not be empty"));
        return;
    }
    if (normalized == m_BinaryPath) {
        return;
    }
    if (streamBusy() || m_Pairing) {
        setLastError(QStringLiteral("Disconnect or cancel pairing before changing the Moonlight executable"));
        return;
    }
    m_BinaryPath = normalized;
    QSettings settings;
    settings.setValue(QStringLiteral("q-sunshine/client/moonlightBinary"), m_BinaryPath);
    emit binaryPathChanged();
}

void MoonlightController::setVideoDecoder(const QString& videoDecoder)
{
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
    m_ProfileHost = settings.value(QStringLiteral("host")).toString().trimmed();
    m_ProfileAppName = settings.value(QStringLiteral("appName"),
                                      QStringLiteral("Desktop")).toString().trimmed();
    m_ProfileResolution = settings.value(QStringLiteral("resolution"),
                                         QStringLiteral("1920x1080")).toString().trimmed();
    m_ProfileDisplayMode = settings.value(QStringLiteral("displayMode"),
                                          QStringLiteral("windowed")).toString().trimmed();
    settings.endGroup();

    if (m_ProfileAppName.isEmpty() || m_ProfileAppName.size() > 256 ||
        hasControlCharacter(m_ProfileAppName)) {
        m_ProfileAppName = QStringLiteral("Desktop");
    }
    if (!isSupportedResolution(m_ProfileResolution)) {
        m_ProfileResolution = QStringLiteral("1920x1080");
    }
    if (!isSupportedDisplayMode(m_ProfileDisplayMode)) {
        m_ProfileDisplayMode = QStringLiteral("windowed");
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
    settings.setValue(QStringLiteral("host"), m_ProfileHost);
    settings.setValue(QStringLiteral("appName"), m_ProfileAppName);
    settings.setValue(QStringLiteral("resolution"), m_ProfileResolution);
    settings.setValue(QStringLiteral("displayMode"), m_ProfileDisplayMode);
    settings.endGroup();
}

bool MoonlightController::selectProfile(const QString& profileId)
{
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
    emit profileChanged();
    return true;
}

bool MoonlightController::saveProfile(const QString& profileId, const QString& host,
                                      const QString& appName, const QString& resolution,
                                      const QString& displayMode)
{
    const QString profileKey = normalizedProfileId(profileId);
    const StreamRequest request {host.trimmed(), appName.trimmed(), resolution.trimmed(),
                                 displayMode.trimmed()};
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
    if (!isSupportedResolution(request.resolution) ||
        !isSupportedDisplayMode(request.displayMode)) {
        setLastError(QStringLiteral("Provide a supported resolution and presentation mode"));
        return false;
    }

    const bool isNewProfile = !m_ProfileIds.contains(profileKey);
    if (isNewProfile) {
        m_ProfileIds.append(profileKey);
    }
    m_CurrentProfileId = profileKey;
    m_ProfileHost = request.host;
    m_ProfileAppName = request.appName;
    m_ProfileResolution = request.resolution;
    m_ProfileDisplayMode = request.displayMode;
    writeCurrentProfile();
    writeProfileIndex();
    setLastError(QString());
    setStatus(QStringLiteral("Desktop profile saved: %1").arg(m_CurrentProfileId));
    if (isNewProfile) {
        emit profilesChanged();
    }
    emit profileChanged();
    return true;
}

bool MoonlightController::validateExecutable(QString* error) const
{
    if (m_BinaryPath.contains(QLatin1Char('/'))) {
        const QFileInfo binary(m_BinaryPath);
        if (!binary.isFile() || !binary.isExecutable()) {
            *error = QStringLiteral("Moonlight binary is not an executable file: %1").arg(m_BinaryPath);
            return false;
        }
        return true;
    }
    if (QStandardPaths::findExecutable(m_BinaryPath).isEmpty()) {
        *error = QStringLiteral("Moonlight executable was not found in PATH: %1").arg(m_BinaryPath);
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
        *error = QStringLiteral("Display mode must be fullscreen, windowed, or borderless");
        return false;
    }
    return validateExecutable(error);
}

void MoonlightController::pair(const QString& host, const QString& pin)
{
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
    StreamRequest request {host.trimmed(), appName.trimmed(), resolution.trimmed(), displayMode.trimmed()};
    QString error;
    if (!validateStreamRequest(request, &error)) {
        setLastError(error);
        return;
    }
    if (m_Pairing || m_PairProcess->state() != QProcess::NotRunning) {
        setLastError(QStringLiteral("Wait for Moonlight pairing to finish before connecting"));
        return;
    }
    const quint64 requestGeneration = ++m_RestartGeneration;
    m_ProfileHost = request.host;
    m_ProfileAppName = request.appName;
    m_ProfileResolution = request.resolution;
    m_ProfileDisplayMode = request.displayMode;
    writeCurrentProfile();
    writeProfileIndex();
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
    QStringList arguments {QStringLiteral("stream"), QStringLiteral("--display-mode"),
                           request.displayMode, QStringLiteral("--resolution"), request.resolution,
                           // A desktop session is never an application that this
                           // shell is allowed to terminate on disconnect.
                           QStringLiteral("--no-quit-after"),
                           QStringLiteral("--absolute-mouse")};
    // The default preserves stock Moonlight's automatic selection.  The two
    // explicit values are constrained enum choices, not arbitrary CLI text.
    if (m_VideoDecoder != QStringLiteral("auto")) {
        arguments << QStringLiteral("--video-decoder") << m_VideoDecoder;
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
