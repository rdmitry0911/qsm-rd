// SPDX-License-Identifier: GPL-3.0-or-later

#include "qsfclient.h"

#include <QAbstractSocket>
#include <QClipboard>
#include <QCryptographicHash>
#include <QFile>
#include <QFileInfo>
#include <QGuiApplication>
#include <QJsonDocument>
#include <QJsonParseError>
#include <QRegularExpression>
#include <QSaveFile>
#include <QSettings>
#include <QSslCertificate>
#include <QSslConfiguration>
#include <QSslKey>
#include <QSslSocket>
#include <QStringList>
#include <QTimer>

#include <cmath>

namespace {

constexpr qint64 kMaxClipboardBytes = 1024 * 1024;
constexpr qint64 kMaxFileBytes = 2 * 1024 * 1024;
constexpr qint64 kMaxResponseBytes = 4 * 1024 * 1024;
constexpr int kMaxQueuedRequests = 8;
constexpr int kClipboardPollIntervalMs = 1500;
constexpr int kClipboardRetryInitialMs = 500;
constexpr int kClipboardRetryMaximumMs = 8000;
constexpr int kRequestTimeoutMs = 15000;

const QRegularExpression kSafeFileName(
    QStringLiteral("\\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\\z"));

QString boundedText(QString value)
{
    value.replace(QLatin1Char('\n'), QLatin1Char(' '));
    value.replace(QLatin1Char('\r'), QLatin1Char(' '));
    value = value.trimmed();
    return value.left(300);
}

QString normalizedProfileId(QString value)
{
    return value.trimmed().normalized(QString::NormalizationForm_C);
}

bool decodeStrictBase64(const QJsonValue& value, qint64 maximum, QByteArray* decoded)
{
    if (!value.isString()) {
        return false;
    }
    const QByteArray input = value.toString().toLatin1();
    if (input.size() % 4 != 0 || input.size() > ((maximum + 2) / 3) * 4) {
        return false;
    }
    for (int index = 0; index < input.size(); ++index) {
        const char character = input.at(index);
        const bool alphabet = (character >= 'A' && character <= 'Z') ||
                              (character >= 'a' && character <= 'z') ||
                              (character >= '0' && character <= '9') ||
                              character == '+' || character == '/';
        const bool padding = character == '=' && index >= input.size() - 2;
        if (!alphabet && !padding) {
            return false;
        }
    }
    const QByteArray result = QByteArray::fromBase64(input);
    if (result.size() > maximum || result.toBase64() != input) {
        return false;
    }
    *decoded = result;
    return true;
}

bool exactInteger(const QJsonValue& value, int minimum, int maximum, int* result)
{
    if (!value.isDouble()) {
        return false;
    }
    const double number = value.toDouble();
    if (!std::isfinite(number) || std::floor(number) != number ||
        number < minimum || number > maximum) {
        return false;
    }
    *result = static_cast<int>(number);
    return true;
}

bool parseResolution(const QString& value, int* width, int* height)
{
    static const QRegularExpression pattern(QStringLiteral("\\A([0-9]{2,5})x([0-9]{2,5})\\z"));
    const QRegularExpressionMatch match = pattern.match(value.trimmed());
    if (!match.hasMatch()) {
        return false;
    }
    bool widthOk = false;
    bool heightOk = false;
    const int parsedWidth = match.captured(1).toInt(&widthOk);
    const int parsedHeight = match.captured(2).toInt(&heightOk);
    if (!widthOk || !heightOk || parsedWidth < 64 || parsedWidth > 16384 ||
        parsedHeight < 64 || parsedHeight > 16384) {
        return false;
    }
    *width = parsedWidth;
    *height = parsedHeight;
    return true;
}

} // namespace

QsfClient::QsfClient(QObject* parent)
    : QObject(parent),
      m_Port(0),
      m_SessionActive(false),
      m_Ready(false),
      m_ClipboardSyncEnabled(false),
      m_InitialClipboardDirection(QStringLiteral("client")),
      m_Status(QStringLiteral("QSF profile is not configured")),
      m_HasActiveRequest(false),
      m_Socket(nullptr),
      m_Clipboard(QGuiApplication::clipboard()),
      m_ClipboardPollTimer(new QTimer(this)),
      m_ClipboardRetryTimer(new QTimer(this)),
      m_RequestTimeoutTimer(new QTimer(this)),
      m_ClipboardGetQueued(false),
      m_ClipboardSetInFlight(false),
      m_ClipboardSetQueued(false),
      m_HasPendingLocalClipboard(false),
      m_ClipboardRetryAttempt(0),
      m_ClipboardRetryEpoch(0),
      m_DesiredResizeWidth(0),
      m_DesiredResizeHeight(0),
      m_LastResizeWidth(0),
      m_LastResizeHeight(0),
      m_ResizeInFlight(false),
      m_ActivationEpoch(0)
{
    m_ProfileId = QStringLiteral("default");
    loadProfile();

    m_ClipboardPollTimer->setInterval(kClipboardPollIntervalMs);
    connect(m_ClipboardPollTimer, &QTimer::timeout, this, &QsfClient::queueClipboardGet);

    m_ClipboardRetryTimer->setSingleShot(true);
    connect(m_ClipboardRetryTimer, &QTimer::timeout, this, [this]() {
        if (m_ClipboardRetryEpoch != m_ActivationEpoch || !m_HasPendingLocalClipboard ||
            !m_ClipboardSyncEnabled || !m_SessionActive || !m_Ready ||
            m_ClipboardSetInFlight || m_ClipboardSetQueued) {
            return;
        }
        // Retry the current snapshot, never an older failed request.
        queueClipboardSet(m_PendingClipboardText);
    });

    m_RequestTimeoutTimer->setSingleShot(true);
    m_RequestTimeoutTimer->setInterval(kRequestTimeoutMs);
    connect(m_RequestTimeoutTimer, &QTimer::timeout, this, [this]() {
        if (m_HasActiveRequest) {
            failActiveRequest(QStringLiteral("QSF gateway request timed out after 15 seconds"));
        }
    });

    if (m_Clipboard != nullptr) {
        connect(m_Clipboard, &QClipboard::dataChanged,
                this, &QsfClient::handleClipboardChanged);
    }
}

QString QsfClient::profileId() const
{
    return m_ProfileId;
}

QString QsfClient::host() const
{
    return m_Host;
}

int QsfClient::port() const
{
    return m_Port;
}

QString QsfClient::serverName() const
{
    return m_ServerName;
}

QString QsfClient::caFile() const
{
    return m_CaFile;
}

QString QsfClient::clientCertificateFile() const
{
    return m_ClientCertificateFile;
}

QString QsfClient::clientKeyFile() const
{
    return m_ClientKeyFile;
}

bool QsfClient::configured() const
{
    return !m_Host.isEmpty() && m_Port >= 1 && m_Port <= 65535 && !m_CaFile.isEmpty() &&
           !m_ClientCertificateFile.isEmpty() && !m_ClientKeyFile.isEmpty();
}

bool QsfClient::sessionActive() const
{
    return m_SessionActive;
}

bool QsfClient::ready() const
{
    return m_Ready;
}

bool QsfClient::clipboardSyncEnabled() const
{
    return m_ClipboardSyncEnabled;
}

QString QsfClient::initialClipboardDirection() const
{
    return m_InitialClipboardDirection;
}

QString QsfClient::status() const
{
    return m_Status;
}

QString QsfClient::lastError() const
{
    return m_LastError;
}

QString QsfClient::lastResult() const
{
    return m_LastResult;
}

QString QsfClient::settingsGroup() const
{
    const QByteArray profileHash = QCryptographicHash::hash(
        m_ProfileId.toUtf8(), QCryptographicHash::Sha256).toHex();
    return QStringLiteral("q-sunshine/qsf/") + QString::fromLatin1(profileHash);
}

void QsfClient::loadProfile()
{
    QSettings settings;
    settings.beginGroup(settingsGroup());
    m_Host = settings.value(QStringLiteral("host")).toString().trimmed();
    m_Port = settings.value(QStringLiteral("port"), 0).toInt();
    m_ServerName = settings.value(QStringLiteral("serverName")).toString().trimmed();
    m_CaFile = settings.value(QStringLiteral("caFile")).toString().trimmed();
    m_ClientCertificateFile =
        settings.value(QStringLiteral("clientCertificateFile")).toString().trimmed();
    m_ClientKeyFile = settings.value(QStringLiteral("clientKeyFile")).toString().trimmed();
    m_ClipboardSyncEnabled = settings.value(QStringLiteral("clipboardSyncEnabled"), false).toBool();
    m_InitialClipboardDirection = settings.value(QStringLiteral("initialClipboardDirection"),
                                                  QStringLiteral("client")).toString().trimmed().toLower();
    if (m_InitialClipboardDirection != QStringLiteral("client") &&
        m_InitialClipboardDirection != QStringLiteral("guest")) {
        m_InitialClipboardDirection = QStringLiteral("client");
    }
    settings.endGroup();
    setStatus(configured() ? QStringLiteral("QSF profile loaded; start a stream to activate it")
                           : QStringLiteral("QSF profile is not configured"));
}

void QsfClient::saveProfile() const
{
    QSettings settings;
    settings.beginGroup(settingsGroup());
    settings.setValue(QStringLiteral("host"), m_Host);
    settings.setValue(QStringLiteral("port"), m_Port);
    settings.setValue(QStringLiteral("serverName"), m_ServerName);
    settings.setValue(QStringLiteral("caFile"), m_CaFile);
    settings.setValue(QStringLiteral("clientCertificateFile"), m_ClientCertificateFile);
    settings.setValue(QStringLiteral("clientKeyFile"), m_ClientKeyFile);
    settings.setValue(QStringLiteral("clipboardSyncEnabled"), m_ClipboardSyncEnabled);
    settings.setValue(QStringLiteral("initialClipboardDirection"), m_InitialClipboardDirection);
    settings.endGroup();
}

void QsfClient::selectProfile(const QString& profileId)
{
    const QString requested = normalizedProfileId(profileId);
    const QString normalized = requested.isEmpty() ? QStringLiteral("default") : requested;
    if (normalized == m_ProfileId) {
        return;
    }

    setSessionActive(false);
    ++m_ActivationEpoch;
    cancelAllRequests();
    m_ProfileId = normalized;
    m_DesiredResizeWidth = 0;
    m_DesiredResizeHeight = 0;
    m_LastResizeWidth = 0;
    m_LastResizeHeight = 0;
    clearPendingClipboard();
    setLastResult(QString());
    loadProfile();
    emit profileChanged();
    emit configurationChanged();
    emit clipboardSyncEnabledChanged();
    emit initialClipboardDirectionChanged();
}

bool QsfClient::applyConfiguration(const QString& host,
                                   int port,
                                   const QString& serverName,
                                   const QString& caFile,
                                   const QString& clientCertificateFile,
                                   const QString& clientKeyFile)
{
    if (m_SessionActive) {
        setLastError(QStringLiteral("Deactivate the QSF companion before changing its endpoint or credentials"));
        return false;
    }
    const QString trimmedHost = host.trimmed();
    if (trimmedHost.isEmpty() || port < 1 || port > 65535) {
        setLastError(QStringLiteral("QSF endpoint must contain a host and a port in 1..65535"));
        return false;
    }

    ++m_ActivationEpoch;
    cancelAllRequests();
    setReady(false);
    m_DesiredResizeWidth = 0;
    m_DesiredResizeHeight = 0;
    m_LastResizeWidth = 0;
    m_LastResizeHeight = 0;
    m_RemoteClipboardHash.clear();
    clearPendingClipboard();
    setLastResult(QString());
    m_Host = trimmedHost;
    m_Port = port;
    m_ServerName = serverName.trimmed();
    m_CaFile = caFile.trimmed();
    m_ClientCertificateFile = clientCertificateFile.trimmed();
    m_ClientKeyFile = clientKeyFile.trimmed();
    saveProfile();
    setLastError(QString());
    setStatus(QStringLiteral("QSF profile saved; press Test or activate it for a visible stream"));
    emit configurationChanged();
    return true;
}

bool QsfClient::applyConfigurationText(const QString& host,
                                       const QString& port,
                                       const QString& serverName,
                                       const QString& caFile,
                                       const QString& clientCertificateFile,
                                       const QString& clientKeyFile)
{
    const QString portText = port.trimmed();
    static const QRegularExpression portPattern(QStringLiteral("\\A[0-9]{1,5}\\z"));
    bool portOk = false;
    const int parsedPort = portText.toInt(&portOk);
    if (!portPattern.match(portText).hasMatch() || !portOk ||
        parsedPort < 1 || parsedPort > 65535) {
        setLastError(QStringLiteral("QSF port must be an integer in 1..65535"));
        return false;
    }
    return applyConfiguration(host, parsedPort, serverName, caFile,
                              clientCertificateFile, clientKeyFile);
}

void QsfClient::setSessionActive(bool active)
{
    if (!active) {
        ++m_ActivationEpoch;
        cancelAllRequests();
        m_ClipboardPollTimer->stop();
        m_RemoteClipboardHash.clear();
        clearPendingClipboard();
        m_DesiredResizeWidth = 0;
        m_DesiredResizeHeight = 0;
        m_LastResizeWidth = 0;
        m_LastResizeHeight = 0;
        m_ResizeInFlight = false;
        setReady(false);
        if (m_SessionActive) {
            m_SessionActive = false;
            emit sessionActiveChanged();
        }
        setStatus(QStringLiteral("QSF inactive: no clipboard, file, or resize request is in flight"));
        return;
    }

    if (m_SessionActive) {
        return;
    }

    if (!configured()) {
        setLastError(QStringLiteral("Configure the QSF endpoint for this profile before using its companion"));
        setStatus(QStringLiteral("QSF stream companion is unavailable"));
        return;
    }

    ++m_ActivationEpoch;
    cancelAllRequests();
    m_RemoteClipboardHash.clear();
    clearPendingClipboard();
    setLastResult(QString());
    m_DesiredResizeWidth = 0;
    m_DesiredResizeHeight = 0;
    m_LastResizeWidth = 0;
    m_LastResizeHeight = 0;
    m_ResizeInFlight = false;
    m_SessionActive = true;
    setReady(false);
    emit sessionActiveChanged();
    setLastError(QString());
    setStatus(QStringLiteral("Verifying QSF gateway for the active stream"));
    enqueue(QStringLiteral("status"), QJsonObject(), QStringLiteral("activation"), true, false);
}

void QsfClient::setClipboardSyncEnabled(bool enabled)
{
    if (m_ClipboardSyncEnabled == enabled) {
        return;
    }
    m_ClipboardSyncEnabled = enabled;
    saveProfile();
    if (!enabled) {
        cancelClipboardRequests();
    }
    updateClipboardPolling();
    if (enabled && m_SessionActive && m_Ready) {
        startClipboardSynchronization();
    }
    emit clipboardSyncEnabledChanged();
}

void QsfClient::setInitialClipboardDirection(const QString& direction)
{
    const QString normalized = direction.trimmed().toLower();
    if (normalized != QStringLiteral("client") && normalized != QStringLiteral("guest")) {
        setLastError(QStringLiteral("Initial clipboard direction must be client or guest"));
        return;
    }
    if (normalized == m_InitialClipboardDirection) {
        return;
    }
    if (m_SessionActive) {
        setLastError(QStringLiteral("Deactivate the QSF companion before changing initial clipboard direction"));
        return;
    }
    m_InitialClipboardDirection = normalized;
    saveProfile();
    emit initialClipboardDirectionChanged();
}

void QsfClient::testConnection()
{
    enqueue(QStringLiteral("status"), QJsonObject(), QStringLiteral("test"), false, false);
}

bool QsfClient::canOperateSession(QString* error) const
{
    if (!configured()) {
        *error = QStringLiteral("Configure the QSF mTLS endpoint first");
        return false;
    }
    if (!m_SessionActive) {
        *error = QStringLiteral("Start the matching Moonlight stream before using QSF data controls");
        return false;
    }
    if (!m_Ready) {
        *error = QStringLiteral("QSF gateway has not completed its stream activation check");
        return false;
    }
    return true;
}

void QsfClient::requestResize(int width, int height)
{
    QString error;
    if (!canOperateSession(&error)) {
        setLastError(error);
        return;
    }
    if (width < 64 || width > 16384 || height < 64 || height > 16384) {
        setLastError(QStringLiteral("QSF resize must be within 64..16384 pixels"));
        return;
    }
    m_DesiredResizeWidth = width;
    m_DesiredResizeHeight = height;
    enqueueLatestResizeIfNeeded();
}

void QsfClient::requestResizeText(const QString& resolution)
{
    int width = 0;
    int height = 0;
    if (!parseResolution(resolution, &width, &height)) {
        setLastError(QStringLiteral("Guest resolution must have WIDTHxHEIGHT form within 64..16384 pixels"));
        return;
    }
    requestResize(width, height);
}

void QsfClient::uploadFile(const QString& sourcePath, const QString& guestName)
{
    QString error;
    if (!canOperateSession(&error)) {
        setLastError(error);
        return;
    }

    QFileInfo sourceInfo(sourcePath);
    const QString name = guestName.trimmed().isEmpty() ? sourceInfo.fileName() : guestName.trimmed();
    if (!validFileName(name)) {
        setLastError(QStringLiteral("QSF file names must be safe ASCII basenames of at most 128 characters"));
        return;
    }
    if (!sourceInfo.isFile() || sourceInfo.size() > kMaxFileBytes) {
        setLastError(QStringLiteral("QSF upload must be a regular file no larger than 2 MiB"));
        return;
    }
    QFile file(sourcePath);
    if (!file.open(QIODevice::ReadOnly)) {
        setLastError(QStringLiteral("Unable to read upload file"));
        return;
    }
    const qint64 expectedSize = sourceInfo.size();
    const QByteArray contents = file.read(kMaxFileBytes + 1);
    if (file.error() != QFileDevice::NoError || contents.size() != expectedSize || !file.atEnd()) {
        setLastError(QStringLiteral("Upload source changed or could not be read completely"));
        return;
    }
    if (contents.size() > kMaxFileBytes) {
        setLastError(QStringLiteral("QSF upload exceeds the 2 MiB limit"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("name"), name);
    payload.insert(QStringLiteral("data_b64"), QString::fromLatin1(contents.toBase64()));
    enqueue(QStringLiteral("upload"), payload, name);
}

void QsfClient::downloadFile(const QString& guestName, const QString& destinationPath)
{
    QString error;
    if (!canOperateSession(&error)) {
        setLastError(error);
        return;
    }
    const QString name = guestName.trimmed();
    if (!validFileName(name) || destinationPath.trimmed().isEmpty()) {
        setLastError(QStringLiteral("Provide a safe QSF guest basename and a local destination path"));
        return;
    }
    QJsonObject payload;
    payload.insert(QStringLiteral("name"), name);
    enqueue(QStringLiteral("download"), payload, destinationPath);
}

bool QsfClient::validateClipboard(const QString& text, QString* error)
{
    const QByteArray bytes = text.toUtf8();
    if (bytes.contains('\0')) {
        *error = QStringLiteral("Clipboard text must not contain NUL");
        return false;
    }
    if (bytes.size() > kMaxClipboardBytes) {
        *error = QStringLiteral("Clipboard text exceeds the QSF 1 MiB limit");
        return false;
    }
    return true;
}

QByteArray QsfClient::clipboardHash(const QString& text)
{
    return QCryptographicHash::hash(text.toUtf8(), QCryptographicHash::Sha256);
}

bool QsfClient::validFileName(const QString& name)
{
    return kSafeFileName.match(name).hasMatch() && name != QStringLiteral(".") &&
           name != QStringLiteral("..") && !name.contains(QStringLiteral(".."));
}

void QsfClient::updateClipboardPolling()
{
    if (m_ClipboardSyncEnabled && m_SessionActive && m_Ready) {
        m_ClipboardPollTimer->start();
    }
    else {
        m_ClipboardPollTimer->stop();
    }
}

void QsfClient::startClipboardSynchronization()
{
    updateClipboardPolling();
    if (!m_ClipboardSyncEnabled || !m_SessionActive || !m_Ready) {
        return;
    }
    if (m_InitialClipboardDirection == QStringLiteral("client") && m_Clipboard != nullptr) {
        // The user explicitly selected client-first synchronization. Keep a
        // local snapshot dirty until the guest acknowledges it, so an old
        // remote value cannot silently overwrite the desktop clipboard.
        queueClipboardSet(m_Clipboard->text(QClipboard::Clipboard));
    }
    else {
        queueClipboardGet();
    }
}

void QsfClient::handleClipboardChanged()
{
    if (!m_ClipboardSyncEnabled || !m_SessionActive || !m_Ready || m_Clipboard == nullptr) {
        return;
    }
    const QString text = m_Clipboard->text(QClipboard::Clipboard);
    if (clipboardHash(text) == m_RemoteClipboardHash) {
        return;
    }
    queueClipboardSet(text);
}

void QsfClient::queueClipboardGet()
{
    if (!m_ClipboardSyncEnabled || !m_SessionActive || !m_Ready ||
        m_Clipboard == nullptr || m_ClipboardGetQueued || m_HasPendingLocalClipboard) {
        return;
    }
    // enqueue() can synchronously start (and, on credential preparation
    // failure, finish) a request. Set the state before calling it so that a
    // synchronous failure cannot be overwritten back to true afterwards.
    m_ClipboardGetQueued = true;
    if (!enqueue(QStringLiteral("clipboard_get"), QJsonObject(),
                 QString(), true, true,
                 clipboardHash(m_Clipboard->text(QClipboard::Clipboard)))) {
        m_ClipboardGetQueued = false;
    }
}

void QsfClient::queueClipboardSet(const QString& text)
{
    QString error;
    if (!canOperateSession(&error) || !validateClipboard(text, &error)) {
        setLastError(error);
        return;
    }

    const QByteArray revision = clipboardHash(text);
    const bool newSnapshot = !m_HasPendingLocalClipboard ||
                             m_PendingClipboardRevision != revision;
    m_PendingClipboardText = text;
    m_PendingClipboardRevision = revision;
    m_HasPendingLocalClipboard = true;
    if (newSnapshot) {
        // A later local copy deserves a fresh, prompt retry budget. An
        // earlier failure must never delay this newer snapshot.
        resetClipboardRetry();
    }
    if (m_ClipboardSetInFlight) {
        return;
    }

    QQueue<Request> retained;
    while (!m_Queue.isEmpty()) {
        Request request = m_Queue.dequeue();
        if (request.operation != QStringLiteral("clipboard_set")) {
            retained.enqueue(request);
        }
    }
    m_Queue = retained;
    m_ClipboardSetQueued = false;

    QJsonObject payload;
    payload.insert(QStringLiteral("text_b64"),
                   QString::fromLatin1(text.toUtf8().toBase64()));
    m_ClipboardSetQueued = true;
    if (!enqueue(QStringLiteral("clipboard_set"), payload, QString(), true, true, revision)) {
        m_ClipboardSetQueued = false;
        scheduleClipboardRetry();
    }
}

void QsfClient::scheduleClipboardRetry()
{
    if (!m_HasPendingLocalClipboard || !m_ClipboardSyncEnabled ||
        !m_SessionActive || !m_Ready || m_ClipboardSetInFlight ||
        m_ClipboardSetQueued || m_ClipboardRetryTimer->isActive()) {
        return;
    }
    const int exponent = qMin(m_ClipboardRetryAttempt, 4);
    const int delay = qMin(kClipboardRetryMaximumMs,
                           kClipboardRetryInitialMs * (1 << exponent));
    ++m_ClipboardRetryAttempt;
    m_ClipboardRetryEpoch = m_ActivationEpoch;
    m_ClipboardRetryTimer->start(delay);
}

void QsfClient::resetClipboardRetry()
{
    m_ClipboardRetryTimer->stop();
    m_ClipboardRetryAttempt = 0;
    m_ClipboardRetryEpoch = 0;
}

void QsfClient::clearPendingClipboard()
{
    resetClipboardRetry();
    m_HasPendingLocalClipboard = false;
    m_PendingClipboardText.clear();
    m_PendingClipboardRevision.clear();
}

bool QsfClient::hasQueuedOperation(const QString& operation) const
{
    for (const Request& request : m_Queue) {
        if (request.operation == operation) {
            return true;
        }
    }
    return false;
}

void QsfClient::enqueueLatestResizeIfNeeded()
{
    if (!m_SessionActive || !m_Ready || m_DesiredResizeWidth <= 0 ||
        m_DesiredResizeHeight <= 0 || m_ResizeInFlight ||
        hasQueuedOperation(QStringLiteral("resize")) ||
        (m_DesiredResizeWidth == m_LastResizeWidth &&
         m_DesiredResizeHeight == m_LastResizeHeight) ||
        m_Queue.size() >= kMaxQueuedRequests) {
        return;
    }
    QJsonObject payload;
    payload.insert(QStringLiteral("width"), m_DesiredResizeWidth);
    payload.insert(QStringLiteral("height"), m_DesiredResizeHeight);
    enqueue(QStringLiteral("resize"), payload);
}

bool QsfClient::enqueue(const QString& operation, const QJsonObject& payload,
                        const QString& context, bool requiresActiveSession,
                        bool requiresReadySession, const QByteArray& clipboardRevision)
{
    if (!configured()) {
        setLastError(QStringLiteral("Configure the QSF mTLS endpoint first"));
        return false;
    }
    if (requiresActiveSession && !m_SessionActive) {
        setLastError(QStringLiteral("QSF request rejected because no matching stream is active"));
        return false;
    }
    if (requiresReadySession && !m_Ready) {
        setLastError(QStringLiteral("QSF request rejected until the active stream gateway is verified"));
        return false;
    }
    if (m_Queue.size() >= kMaxQueuedRequests) {
        setLastError(QStringLiteral("QSF request queue is full"));
        return false;
    }

    Request request;
    request.operation = operation;
    request.payload = payload;
    request.payload.insert(QStringLiteral("op"), operation);
    request.context = context;
    request.requiresActiveSession = requiresActiveSession;
    request.requiresReadySession = requiresReadySession;
    request.activationEpoch = m_ActivationEpoch;
    request.clipboardRevision = clipboardRevision;
    m_Queue.enqueue(request);
    startNextRequest();
    return true;
}

bool QsfClient::prepareSslSocket(QSslSocket* socket, QString* error) const
{
    QFile caFile(m_CaFile);
    QFile certificateFile(m_ClientCertificateFile);
    QFile keyFile(m_ClientKeyFile);
    if (!caFile.open(QIODevice::ReadOnly) || !certificateFile.open(QIODevice::ReadOnly) ||
        !keyFile.open(QIODevice::ReadOnly)) {
        *error = QStringLiteral("Unable to read QSF CA or client credential files");
        return false;
    }

    const QList<QSslCertificate> cas = QSslCertificate::fromData(caFile.readAll(), QSsl::Pem);
    const QList<QSslCertificate> certificates =
        QSslCertificate::fromData(certificateFile.readAll(), QSsl::Pem);
    const QByteArray privateKeyBytes = keyFile.readAll();
    QSslKey privateKey(privateKeyBytes, QSsl::Rsa, QSsl::Pem, QSsl::PrivateKey);
    if (privateKey.isNull()) {
        privateKey = QSslKey(privateKeyBytes, QSsl::Ec, QSsl::Pem, QSsl::PrivateKey);
    }
    if (cas.isEmpty() || certificates.isEmpty() || privateKey.isNull()) {
        *error = QStringLiteral("Invalid QSF CA, client certificate, or unencrypted RSA/EC private key");
        return false;
    }

    QSslConfiguration configuration = QSslConfiguration::defaultConfiguration();
    configuration.setProtocol(QSsl::TlsV1_3OrLater);
    configuration.setCaCertificates(cas);
    configuration.setLocalCertificateChain(certificates);
    configuration.setPrivateKey(privateKey);
    socket->setSslConfiguration(configuration);
    socket->setPeerVerifyMode(QSslSocket::VerifyPeer);
    socket->setPeerVerifyName(m_ServerName.isEmpty() ? m_Host : m_ServerName);
    return true;
}

void QsfClient::startNextRequest()
{
    if (m_HasActiveRequest) {
        return;
    }

    while (!m_Queue.isEmpty()) {
        const Request candidate = m_Queue.dequeue();
        if (candidate.activationEpoch != m_ActivationEpoch ||
            (candidate.requiresActiveSession && !m_SessionActive) ||
            (candidate.requiresReadySession && !m_Ready)) {
            continue;
        }
        m_ActiveRequest = candidate;
        m_HasActiveRequest = true;
        m_ResponseBuffer.clear();
        if (m_ActiveRequest.operation == QStringLiteral("clipboard_set")) {
            m_ClipboardSetQueued = false;
            m_ClipboardSetInFlight = true;
        }
        else if (m_ActiveRequest.operation == QStringLiteral("resize")) {
            m_ResizeInFlight = true;
        }

        auto* socket = new QSslSocket(this);
        socket->setReadBufferSize(kMaxResponseBytes + 1);
        m_Socket = socket;
        QString error;
        if (!prepareSslSocket(socket, &error)) {
            failActiveRequest(error);
            return;
        }

        connect(socket, &QSslSocket::encrypted, this, [this, socket]() {
            if (socket != m_Socket || !m_HasActiveRequest) {
                return;
            }
            const QByteArray request = QJsonDocument(m_ActiveRequest.payload)
                                           .toJson(QJsonDocument::Compact) + '\n';
            socket->write(request);
        });
        connect(socket, &QSslSocket::readyRead, this, [this, socket]() { drainResponse(socket); });
        connect(socket, &QSslSocket::sslErrors, this,
                [this, socket](const QList<QSslError>& errors) {
                    if (socket != m_Socket || !m_HasActiveRequest) {
                        return;
                    }
                    QStringList messages;
                    for (const QSslError& sslError : errors) {
                        messages.append(boundedText(sslError.errorString()));
                    }
                    failActiveRequest(QStringLiteral("QSF TLS verification failed: %1")
                                          .arg(messages.join(QStringLiteral("; "))));
                });
        connect(socket, &QSslSocket::errorOccurred, this,
                [this, socket](QAbstractSocket::SocketError socketError) {
                    if (socket == m_Socket && m_HasActiveRequest) {
                        // A TLS peer can send the complete one-line reply and
                        // immediately close. Give buffered plaintext a chance
                        // to be parsed before treating that close as failure.
                        if (socketError == QAbstractSocket::RemoteHostClosedError) {
                            drainResponse(socket);
                            return;
                        }
                        failActiveRequest(QStringLiteral("QSF connection failed: %1")
                                              .arg(boundedText(socket->errorString())));
                    }
                });
        connect(socket, &QSslSocket::disconnected, this, [this, socket]() {
            if (socket == m_Socket && m_HasActiveRequest) {
                drainResponse(socket);
            }
            if (socket == m_Socket && m_HasActiveRequest) {
                failActiveRequest(QStringLiteral("QSF gateway disconnected before responding"));
            }
        });

        setStatus(QStringLiteral("QSF %1 in progress").arg(m_ActiveRequest.operation));
        m_RequestTimeoutTimer->start();
        const QString peerName = m_ServerName.isEmpty() ? m_Host : m_ServerName;
        socket->connectToHostEncrypted(m_Host, static_cast<quint16>(m_Port), peerName);
        return;
    }
}

void QsfClient::drainResponse(QSslSocket* socket)
{
    if (socket != m_Socket || !m_HasActiveRequest) {
        return;
    }
    m_ResponseBuffer.append(socket->readAll());
    if (m_ResponseBuffer.size() > kMaxResponseBytes) {
        failActiveRequest(QStringLiteral("QSF gateway response exceeded 4 MiB"));
        return;
    }
    const int newline = m_ResponseBuffer.indexOf('\n');
    if (newline < 0) {
        return;
    }
    if (!m_ResponseBuffer.mid(newline + 1).isEmpty()) {
        failActiveRequest(QStringLiteral("QSF gateway returned more than one response line"));
        return;
    }
    QJsonParseError parseError;
    const QJsonDocument document = QJsonDocument::fromJson(
        m_ResponseBuffer.left(newline), &parseError);
    if (parseError.error != QJsonParseError::NoError || !document.isObject()) {
        failActiveRequest(QStringLiteral("QSF gateway returned invalid JSON"));
        return;
    }
    finishActiveRequest(document.object());
}

void QsfClient::finishActiveRequest(const QJsonObject& response)
{
    const Request completed = m_ActiveRequest;
    m_RequestTimeoutTimer->stop();
    if (m_Socket != nullptr) {
        QSslSocket* socket = m_Socket;
        m_Socket = nullptr;
        socket->disconnect(this);
        socket->disconnectFromHost();
        socket->deleteLater();
    }

    if (completed.activationEpoch != m_ActivationEpoch ||
        (completed.requiresActiveSession && !m_SessionActive)) {
        m_HasActiveRequest = false;
        m_ClipboardGetQueued = false;
        m_ClipboardSetInFlight = false;
        m_ResizeInFlight = false;
        startNextRequest();
        return;
    }
    if (!response.value(QStringLiteral("ok")).isBool() ||
        !response.value(QStringLiteral("ok")).toBool()) {
        const QJsonValue errorValue = response.value(QStringLiteral("error"));
        failActiveRequest(QStringLiteral("QSF gateway rejected the request: %1")
                              .arg(errorValue.isString() ? boundedText(errorValue.toString())
                                                          : QStringLiteral("unspecified error")));
        return;
    }
    const QJsonValue resultValue = response.value(QStringLiteral("result"));
    if (!resultValue.isObject()) {
        failActiveRequest(QStringLiteral("QSF gateway response has no result object"));
        return;
    }
    const QJsonObject result = resultValue.toObject();

    if (completed.operation == QStringLiteral("status")) {
        const QJsonValue agent = result.value(QStringLiteral("agent"));
        if (!agent.isString() || agent.toString() != QStringLiteral("ready")) {
            failActiveRequest(QStringLiteral("QSF gateway returned an invalid status result"));
            return;
        }
        const QString agentName = QStringLiteral("ready");
        if (completed.context == QStringLiteral("activation")) {
            setReady(true);
            startClipboardSynchronization();
            enqueueLatestResizeIfNeeded();
            setStatus(QStringLiteral("QSF gateway ready for this stream (%1)").arg(agentName));
        }
        else {
            setStatus(QStringLiteral("QSF agent: %1").arg(agentName));
        }
    }
    else if (completed.operation == QStringLiteral("clipboard_get")) {
        m_ClipboardGetQueued = false;
        if (!handleClipboardReply(result, completed.clipboardRevision)) {
            failActiveRequest(QStringLiteral("QSF returned invalid clipboard text"));
            return;
        }
        emit clipboardReceivedFromGuest();
    }
    else if (completed.operation == QStringLiteral("clipboard_set")) {
        m_ClipboardSetInFlight = false;
        int bytes = 0;
        const QByteArray expected = completed.payload.value(QStringLiteral("text_b64"))
                                        .toString().toLatin1();
        const int expectedBytes = QByteArray::fromBase64(expected).size();
        if (!exactInteger(result.value(QStringLiteral("bytes")), 0,
                          static_cast<int>(kMaxClipboardBytes), &bytes) ||
            bytes != expectedBytes) {
            failActiveRequest(QStringLiteral("QSF returned an invalid clipboard-set result"));
            return;
        }
        m_RemoteClipboardHash = completed.clipboardRevision;
        setStatus(QStringLiteral("Clipboard text accepted by QSF guest state"));
        emit clipboardSentToGuest();
        if (m_HasPendingLocalClipboard &&
            m_PendingClipboardRevision == completed.clipboardRevision) {
            clearPendingClipboard();
            // Resume guest polling only after this local revision is
            // acknowledged, so a stale guest response cannot undo it.
            queueClipboardGet();
        }
        else if (m_HasPendingLocalClipboard) {
            // An older in-flight set was accepted after a newer local copy.
            // Dispatch only the newer snapshot.
            queueClipboardSet(m_PendingClipboardText);
        }
    }
    else if (completed.operation == QStringLiteral("resize")) {
        m_ResizeInFlight = false;
        const int expectedWidth = completed.payload.value(QStringLiteral("width")).toInt();
        const int expectedHeight = completed.payload.value(QStringLiteral("height")).toInt();
        int width = 0;
        int height = 0;
        const QJsonValue qemuResult = result.value(QStringLiteral("qemu_set_ui_info"));
        if (!exactInteger(result.value(QStringLiteral("width")), 64, 16384, &width) ||
            !exactInteger(result.value(QStringLiteral("height")), 64, 16384, &height) ||
            width != expectedWidth || height != expectedHeight || !qemuResult.isString() ||
            (qemuResult.toString() != QStringLiteral("applied") &&
             qemuResult.toString() != QStringLiteral("disabled"))) {
            failActiveRequest(QStringLiteral("QSF returned an invalid resize result"));
            return;
        }
        const bool qemuApplied = qemuResult.toString() == QStringLiteral("applied");
        // A valid reply means the guest accepted this resolution even if this
        // gateway deliberately has QEMU SetUIInfo disabled. Remember it so a
        // completed request is not immediately queued forever.
        m_LastResizeWidth = width;
        m_LastResizeHeight = height;
        if (qemuApplied) {
            setStatus(QStringLiteral("QSF resize requested; waiting for a new video scanout"));
        }
        else {
            setStatus(QStringLiteral("QSF guest resize stored but QEMU did not apply SetUIInfo"));
        }
        setLastResult(QStringLiteral("Guest resize request accepted: %1x%2 (%3)")
                          .arg(width).arg(height).arg(qemuResult.toString()));
        emit resizeApplied(width, height, qemuApplied);
    }
    else if (completed.operation == QStringLiteral("upload")) {
        int bytes = 0;
        const QJsonValue name = result.value(QStringLiteral("name"));
        const QByteArray payload = QByteArray::fromBase64(
            completed.payload.value(QStringLiteral("data_b64")).toString().toLatin1());
        if (!name.isString() || name.toString() != completed.context || !validFileName(name.toString()) ||
            !exactInteger(result.value(QStringLiteral("bytes")), 0,
                          static_cast<int>(kMaxFileBytes), &bytes) || bytes != payload.size()) {
            failActiveRequest(QStringLiteral("QSF returned an invalid upload result"));
            return;
        }
        setStatus(QStringLiteral("Uploaded %1 (%2 bytes) to guest inbox").arg(name.toString()).arg(bytes));
        setLastResult(m_Status);
        emit fileTransferFinished(m_Status);
    }
    else if (completed.operation == QStringLiteral("download")) {
        const QString expectedName = completed.payload.value(QStringLiteral("name")).toString();
        const QJsonValue name = result.value(QStringLiteral("name"));
        QByteArray decoded;
        int bytes = 0;
        if (!name.isString() || name.toString() != expectedName || !validFileName(name.toString()) ||
            !decodeStrictBase64(result.value(QStringLiteral("data_b64")), kMaxFileBytes, &decoded) ||
            !exactInteger(result.value(QStringLiteral("bytes")), 0,
                          static_cast<int>(kMaxFileBytes), &bytes) || bytes != decoded.size()) {
            failActiveRequest(QStringLiteral("QSF returned an invalid download result"));
            return;
        }
        QSaveFile destination(completed.context);
        if (!destination.open(QIODevice::WriteOnly) ||
            destination.write(decoded) != decoded.size() || !destination.commit()) {
            failActiveRequest(QStringLiteral("Unable to atomically save QSF download"));
            return;
        }
        setStatus(QStringLiteral("Downloaded %1 bytes from guest outbox").arg(decoded.size()));
        setLastResult(m_Status);
        emit fileTransferFinished(m_Status);
    }
    else {
        failActiveRequest(QStringLiteral("QSF client received an unexpected operation reply"));
        return;
    }

    // A periodic clipboard poll must not erase an error from a file or
    // resize operation. Only an explicit successful operation (or a gateway
    // activation/test) clears the actionable error state.
    if (completed.operation == QStringLiteral("upload") ||
        completed.operation == QStringLiteral("download") ||
        completed.operation == QStringLiteral("resize") ||
        (completed.operation == QStringLiteral("status") &&
         (completed.context == QStringLiteral("activation") ||
          completed.context == QStringLiteral("test")))) {
        setLastError(QString());
    }
    m_HasActiveRequest = false;
    // A resize can have been deferred only because the bounded operation
    // queue was full. Reconsider its latest coalesced value whenever any
    // successful request frees a slot, not only after another resize.
    enqueueLatestResizeIfNeeded();
    startNextRequest();
}

bool QsfClient::handleClipboardReply(const QJsonObject& result,
                                     const QByteArray& requestLocalRevision)
{
    if (m_Clipboard == nullptr || !m_ClipboardSyncEnabled || !m_SessionActive || !m_Ready) {
        return true;
    }
    QByteArray bytes;
    if (!decodeStrictBase64(result.value(QStringLiteral("text_b64")), kMaxClipboardBytes, &bytes) ||
        bytes.contains('\0')) {
        return false;
    }
    const QString text = QString::fromUtf8(bytes.constData(), bytes.size());
    if (text.toUtf8() != bytes) {
        return false;
    }

    const QByteArray currentLocalRevision =
        clipboardHash(m_Clipboard->text(QClipboard::Clipboard));
    const QByteArray remoteRevision = clipboardHash(text);
    if (m_HasPendingLocalClipboard) {
        if (currentLocalRevision != m_PendingClipboardRevision) {
            // A platform clipboard change raced the poll without a usable
            // dataChanged delivery. Treat the currently visible text as the
            // newest local snapshot rather than overwriting it with remote.
            queueClipboardSet(m_Clipboard->text(QClipboard::Clipboard));
        }
        else if (remoteRevision == m_PendingClipboardRevision) {
            // The guest already has the exact local snapshot. This reply is
            // a valid acknowledgement even if the prior set response was
            // lost, so no retry is needed.
            m_RemoteClipboardHash = remoteRevision;
            clearPendingClipboard();
        }
        else if (!m_ClipboardSetInFlight && !m_ClipboardSetQueued) {
            queueClipboardSet(m_PendingClipboardText);
        }
        // Never let a poll response overwrite a local snapshot awaiting
        // acknowledgement.
        return true;
    }
    if (currentLocalRevision != requestLocalRevision && currentLocalRevision != remoteRevision) {
        // A local copy happened while this poll was on the wire. Never let a
        // stale guest reply overwrite it; send the newer local value instead.
        queueClipboardSet(m_Clipboard->text(QClipboard::Clipboard));
        return true;
    }
    m_RemoteClipboardHash = remoteRevision;
    if (currentLocalRevision != remoteRevision) {
        m_Clipboard->setText(text, QClipboard::Clipboard);
    }
    return true;
}

void QsfClient::cancelClipboardRequests()
{
    QQueue<Request> retained;
    while (!m_Queue.isEmpty()) {
        Request request = m_Queue.dequeue();
        if (request.operation != QStringLiteral("clipboard_get") &&
            request.operation != QStringLiteral("clipboard_set")) {
            retained.enqueue(request);
        }
    }
    m_Queue = retained;
    m_ClipboardGetQueued = false;
    m_ClipboardSetQueued = false;
    clearPendingClipboard();
    if (m_HasActiveRequest &&
        (m_ActiveRequest.operation == QStringLiteral("clipboard_get") ||
         m_ActiveRequest.operation == QStringLiteral("clipboard_set"))) {
        m_RequestTimeoutTimer->stop();
        if (m_Socket != nullptr) {
            QSslSocket* socket = m_Socket;
            m_Socket = nullptr;
            socket->disconnect(this);
            socket->abort();
            socket->deleteLater();
        }
        m_HasActiveRequest = false;
        m_ClipboardSetInFlight = false;
        m_ActiveRequest = Request();
    }
    QTimer::singleShot(0, this, [this]() { startNextRequest(); });
}

void QsfClient::cancelAllRequests()
{
    m_RequestTimeoutTimer->stop();
    m_ClipboardPollTimer->stop();
    m_Queue.clear();
    m_ResponseBuffer.clear();
    m_ClipboardGetQueued = false;
    m_ClipboardSetQueued = false;
    m_ClipboardSetInFlight = false;
    clearPendingClipboard();
    m_ResizeInFlight = false;
    if (m_Socket != nullptr) {
        QSslSocket* socket = m_Socket;
        m_Socket = nullptr;
        socket->disconnect(this);
        socket->abort();
        socket->deleteLater();
    }
    m_HasActiveRequest = false;
    m_ActiveRequest = Request();
}

void QsfClient::failActiveRequest(const QString& error)
{
    const Request failed = m_ActiveRequest;
    const bool retryLatestResize =
        failed.operation != QStringLiteral("resize") ||
        m_DesiredResizeWidth != failed.payload.value(QStringLiteral("width")).toInt() ||
        m_DesiredResizeHeight != failed.payload.value(QStringLiteral("height")).toInt();
    m_RequestTimeoutTimer->stop();
    if (m_Socket != nullptr) {
        QSslSocket* socket = m_Socket;
        m_Socket = nullptr;
        socket->disconnect(this);
        socket->abort();
        socket->deleteLater();
    }
    m_HasActiveRequest = false;
    if (failed.operation == QStringLiteral("clipboard_get")) {
        m_ClipboardGetQueued = false;
    }
    if (failed.operation == QStringLiteral("clipboard_set")) {
        m_ClipboardSetInFlight = false;
        m_ClipboardSetQueued = false;
        // Retain the newest local snapshot and retry it with bounded
        // exponential backoff. A subsequent local dataChanged replaces it.
        scheduleClipboardRetry();
    }
    if (failed.operation == QStringLiteral("resize")) {
        m_ResizeInFlight = false;
    }
    if (failed.context == QStringLiteral("activation")) {
        setReady(false);
        updateClipboardPolling();
    }
    setLastError(boundedText(error));
    setStatus(QStringLiteral("QSF error: %1").arg(m_LastError));
    QTimer::singleShot(0, this, [this, retryLatestResize]() {
        // Do not spin on a failing resize request, but do preserve a newer
        // user request B when older in-flight request A failed.
        if (retryLatestResize) {
            enqueueLatestResizeIfNeeded();
        }
        startNextRequest();
    });
}

void QsfClient::setReady(bool ready)
{
    if (m_Ready != ready) {
        m_Ready = ready;
        emit readyChanged();
    }
}

void QsfClient::setStatus(const QString& status)
{
    const QString normalized = boundedText(status);
    if (m_Status != normalized) {
        m_Status = normalized;
        emit statusChanged();
    }
}

void QsfClient::setLastError(const QString& error)
{
    const QString normalized = boundedText(error);
    if (m_LastError != normalized) {
        m_LastError = normalized;
        emit lastErrorChanged();
    }
}

void QsfClient::setLastResult(const QString& result)
{
    const QString normalized = boundedText(result);
    if (m_LastResult != normalized) {
        m_LastResult = normalized;
        emit lastResultChanged();
    }
}
