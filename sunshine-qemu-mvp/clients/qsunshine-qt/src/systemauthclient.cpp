// SPDX-License-Identifier: GPL-3.0-or-later

#include "systemauthclient.h"

#include "qsfclient.h"

#include <QAbstractSocket>
#include <QCryptographicHash>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonParseError>
#include <QRegularExpression>
#include <QSettings>
#include <QSslCertificate>
#include <QSslConfiguration>
#include <QSslSocket>
#include <QTimer>

#include <climits>
#include <cmath>

namespace {

constexpr int kMaxResponseBytes = 8 * 1024;
constexpr int kRequestTimeoutMs = 15000;
constexpr int kClockGuardIntervalMs = 1000;
constexpr qint64 kMaximumTicketLifetimeMs = 15 * 60 * 1000;
constexpr qint64 kMaximumClockSkewMs = 30 * 1000;

QString normalizedProfileId(QString value)
{
    return value.trimmed().normalized(QString::NormalizationForm_C);
}

QString boundedText(QString value)
{
    value.replace(QLatin1Char('\n'), QLatin1Char(' '));
    value.replace(QLatin1Char('\r'), QLatin1Char(' '));
    return value.trimmed().left(300);
}

bool exactInteger(const QJsonValue& value, qint64 minimum, qint64 maximum,
                  qint64* result)
{
    if (!value.isDouble()) {
        return false;
    }
    const double number = value.toDouble();
    if (!std::isfinite(number) || std::floor(number) != number ||
        number < static_cast<double>(minimum) || number > static_cast<double>(maximum)) {
        return false;
    }
    *result = static_cast<qint64>(number);
    return true;
}

bool validAudience(const QString& audience)
{
    static const QRegularExpression pattern(
        QStringLiteral("\\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\\z"));
    return pattern.match(audience).hasMatch();
}

} // namespace

SystemAuthClient::SystemAuthClient(QObject* parent)
    : QObject(parent),
      m_Port(0),
      m_Authenticating(false),
      m_ExpiresAtUtcMs(0),
      m_Socket(nullptr),
      m_RequestTimeoutTimer(new QTimer(this)),
      m_ExpiryTimer(new QTimer(this)),
      m_ClockGuardTimer(new QTimer(this)),
      m_QsfClient(nullptr)
{
    m_ProfileId = QStringLiteral("default");
    loadProfile();
    m_RequestTimeoutTimer->setSingleShot(true);
    m_RequestTimeoutTimer->setInterval(kRequestTimeoutMs);
    connect(m_RequestTimeoutTimer, &QTimer::timeout, this, [this]() {
        if (m_Authenticating) {
            failLogin(QStringLiteral("System authentication timed out"));
        }
    });
    m_ExpiryTimer->setSingleShot(true);
    connect(m_ExpiryTimer, &QTimer::timeout, this, &SystemAuthClient::expireSession);
    // QTimer is monotonic relative to when the login completed. Check wall
    // time as well so a forward system-clock change cannot leave the
    // Q_PROPERTY/admission consumer with a stale ticket until that original
    // monotonic deadline. The server-issued expiry remains the authority.
    m_ClockGuardTimer->setInterval(kClockGuardIntervalMs);
    connect(m_ClockGuardTimer, &QTimer::timeout, this, [this]() {
        if (!m_Ticket.isEmpty() &&
            m_ExpiresAtUtcMs <= QDateTime::currentMSecsSinceEpoch()) {
            expireSession();
        }
    });
}

QString SystemAuthClient::profileId() const
{
    return m_ProfileId;
}

QString SystemAuthClient::host() const
{
    return m_Host;
}

int SystemAuthClient::port() const
{
    return m_Port;
}

QString SystemAuthClient::serverName() const
{
    return m_ServerName;
}

QString SystemAuthClient::caFile() const
{
    return m_CaFile;
}

QString SystemAuthClient::expectedAudience() const
{
    return m_ExpectedAudience;
}

bool SystemAuthClient::configured() const
{
    return !m_Host.isEmpty() && m_Port >= 1 && m_Port <= 65535 && !m_CaFile.isEmpty() &&
           validAudience(m_ExpectedAudience);
}

bool SystemAuthClient::authenticating() const
{
    return m_Authenticating;
}

bool SystemAuthClient::authenticated() const
{
    return !m_Ticket.isEmpty() && m_ExpiresAtUtcMs > QDateTime::currentMSecsSinceEpoch();
}

QString SystemAuthClient::subject() const
{
    return authenticated() ? m_Subject : QString();
}

QDateTime SystemAuthClient::expiresAtUtc() const
{
    return authenticated() ? QDateTime::fromMSecsSinceEpoch(m_ExpiresAtUtcMs, Qt::UTC)
                           : QDateTime();
}

QString SystemAuthClient::status() const
{
    return m_Status;
}

QString SystemAuthClient::lastError() const
{
    return m_LastError;
}

QString SystemAuthClient::settingsGroup() const
{
    const QByteArray hash = QCryptographicHash::hash(
        m_ProfileId.toUtf8(), QCryptographicHash::Sha256).toHex();
    return QStringLiteral("q-sunshine/system-auth/") + QString::fromLatin1(hash);
}

bool SystemAuthClient::validProfileId(const QString& profileId)
{
    if (profileId.isEmpty() || profileId.size() > 64) {
        return false;
    }
    for (const QChar character : profileId) {
        if (character.isNull() || character.category() == QChar::Other_Control) {
            return false;
        }
    }
    return true;
}

bool SystemAuthClient::validUsername(const QString& username)
{
    static const QRegularExpression pattern(
        QStringLiteral("\\A[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}\\z"));
    return pattern.match(username).hasMatch();
}

void SystemAuthClient::loadProfile()
{
    QSettings settings;
    settings.beginGroup(settingsGroup());
    m_Host = settings.value(QStringLiteral("host")).toString().trimmed();
    m_Port = settings.value(QStringLiteral("port"), 0).toInt();
    m_ServerName = settings.value(QStringLiteral("serverName")).toString().trimmed();
    m_CaFile = settings.value(QStringLiteral("caFile")).toString().trimmed();
    m_ExpectedAudience = settings.value(QStringLiteral("expectedAudience")).toString().trimmed();
    settings.endGroup();
    if (m_Port < 1 || m_Port > 65535) {
        m_Port = 0;
    }
    if (!validAudience(m_ExpectedAudience)) {
        m_ExpectedAudience.clear();
    }
    setStatus(configured() ? QStringLiteral("System-auth profile loaded; sign in before connecting")
                           : QStringLiteral("System-auth profile is not configured"));
}

void SystemAuthClient::saveProfile() const
{
    QSettings settings;
    settings.beginGroup(settingsGroup());
    // Do not add password, ticket, subject, or expiry fields here. A profile
    // contains routing/trust/audience metadata only.
    settings.setValue(QStringLiteral("host"), m_Host);
    settings.setValue(QStringLiteral("port"), m_Port);
    settings.setValue(QStringLiteral("serverName"), m_ServerName);
    settings.setValue(QStringLiteral("caFile"), m_CaFile);
    settings.setValue(QStringLiteral("expectedAudience"), m_ExpectedAudience);
    settings.endGroup();
}

bool SystemAuthClient::selectProfile(const QString& profileId)
{
    const QString normalized = normalizedProfileId(profileId);
    if (!validProfileId(normalized)) {
        setLastError(QStringLiteral("System-auth profile name must contain 1 to 64 printable characters"));
        return false;
    }
    if (normalized == m_ProfileId) {
        return true;
    }
    // An outstanding response from the old profile must never be able to
    // attach a ticket after the user switches VMs.
    logout();
    m_ProfileId = normalized;
    loadProfile();
    emit profileChanged();
    emit configurationChanged();
    return true;
}

bool SystemAuthClient::applyConfiguration(const QString& host, int port,
                                          const QString& serverName,
                                          const QString& caFile,
                                          const QString& expectedAudience)
{
    const QString normalizedHost = host.trimmed();
    const QString normalizedAudience = expectedAudience.trimmed();
    if (normalizedHost.isEmpty() || normalizedHost.size() > 255 || port < 1 || port > 65535 ||
        caFile.trimmed().isEmpty() || !validAudience(normalizedAudience)) {
        setLastError(QStringLiteral("System-auth endpoint requires a host, port in 1..65535, CA file, and expected VM audience"));
        return false;
    }
    const QString normalizedServerName = serverName.trimmed();
    const QString normalizedCaFile = caFile.trimmed();
    // Retain an active ticket for an idempotent Save action. A changed route,
    // SNI, trust anchor, or expected VM audience can target another
    // authorization boundary and therefore always cancels the old login
    // before replacing configuration.
    const bool endpointChanged = m_Host != normalizedHost || m_Port != port ||
                                 m_ServerName != normalizedServerName ||
                                 m_CaFile != normalizedCaFile ||
                                 m_ExpectedAudience != normalizedAudience;
    if (endpointChanged) {
        logout();
    }
    m_Host = normalizedHost;
    m_Port = port;
    m_ServerName = normalizedServerName;
    m_CaFile = normalizedCaFile;
    m_ExpectedAudience = normalizedAudience;
    saveProfile();
    setLastError(QString());
    setStatus(QStringLiteral("System-auth profile saved; credentials will not be stored"));
    emit configurationChanged();
    return true;
}

bool SystemAuthClient::applyConfigurationText(const QString& host, const QString& port,
                                              const QString& serverName, const QString& caFile,
                                              const QString& expectedAudience)
{
    static const QRegularExpression portPattern(QStringLiteral("\\A[0-9]{1,5}\\z"));
    bool parsed = false;
    const QString normalizedPort = port.trimmed();
    const int value = normalizedPort.toInt(&parsed);
    if (!parsed || !portPattern.match(normalizedPort).hasMatch() || value < 1 || value > 65535) {
        setLastError(QStringLiteral("System-auth port must be an integer in 1..65535"));
        return false;
    }
    return applyConfiguration(host, value, serverName, caFile, expectedAudience);
}

void SystemAuthClient::attachQsfClient(QsfClient* client)
{
    if (m_QsfClient == client) {
        return;
    }
    if (m_QsfClient != nullptr) {
        disconnect(m_QsfClient, nullptr, this, nullptr);
        m_QsfClient->clearEphemeralSystemAuthTicket();
    }
    m_QsfClient = client;
    if (m_QsfClient == nullptr) {
        return;
    }
    connect(m_QsfClient, &QsfClient::profileChanged, this, [this]() {
        if (m_QsfClient != nullptr && m_QsfClient->profileId() != m_ProfileId) {
            selectProfile(m_QsfClient->profileId());
        }
    });
    connect(m_QsfClient, &QsfClient::configurationChanged, this, [this]() {
        if (m_QsfClient == nullptr || m_QsfClient->hasValidEphemeralSystemAuthTicket()) {
            return;
        }
        // A QSF endpoint, SNI, CA, or profile transition can cause QsfClient
        // to drop its profile/audience-scoped ticket.  Do not leave the
        // higher-level launch admission alive for that now-unbound session.
        // Cover an in-flight login too: its response must not attach a ticket
        // to a route the user changed while authentication was pending.
        if (authenticated() || authenticating()) {
            logout();
        }
    });
    if (authenticated()) {
        m_QsfClient->setEphemeralSystemAuthTicket(m_Ticket, m_ExpiresAtUtcMs);
    }
}

void SystemAuthClient::setGameStreamLeaseSink(GameStreamLeaseSink installLease,
                                              std::function<void()> clearLease)
{
    if (m_ClearGameStreamLease) {
        m_ClearGameStreamLease();
    }
    m_InstallGameStreamLease = std::move(installLease);
    m_ClearGameStreamLease = std::move(clearLease);
    if (authenticated() && m_InstallGameStreamLease) {
        m_InstallGameStreamLease(m_Host, m_Port, m_ServerName, m_CaFile,
                                 m_ExpectedAudience, m_Ticket, m_ExpiresAtUtcMs);
    }
}

bool SystemAuthClient::prepareSslSocket(QSslSocket* socket, QString* error) const
{
    QFile file(m_CaFile);
    if (!file.open(QIODevice::ReadOnly)) {
        *error = QStringLiteral("Unable to read system-auth CA certificate file");
        return false;
    }
    const QList<QSslCertificate> certificates = QSslCertificate::fromData(file.readAll(), QSsl::Pem);
    if (certificates.isEmpty()) {
        *error = QStringLiteral("Invalid system-auth CA certificate file");
        return false;
    }
    QSslConfiguration configuration = QSslConfiguration::defaultConfiguration();
    configuration.setProtocol(QSsl::TlsV1_3OrLater);
    configuration.setCaCertificates(certificates);
    socket->setSslConfiguration(configuration);
    socket->setPeerVerifyMode(QSslSocket::VerifyPeer);
    socket->setPeerVerifyName(m_ServerName.isEmpty() ? m_Host : m_ServerName);
    return true;
}

void SystemAuthClient::login(const QString& username, const QString& password)
{
    const QString normalizedUsername = username.trimmed();
    QByteArray passwordBytes = password.toUtf8();
    if (!configured()) {
        setLastError(QStringLiteral("Configure the system-auth endpoint before signing in"));
        return;
    }
    if (m_Authenticating) {
        setLastError(QStringLiteral("A system-authentication request is already in progress"));
        return;
    }
    if (!validUsername(normalizedUsername) || password.isEmpty() || password.contains(QChar::Null) ||
        passwordBytes.size() > 4096) {
        passwordBytes.fill('\0');
        setLastError(QStringLiteral("Provide a valid system username and password"));
        return;
    }
    passwordBytes.fill('\0');

    clearSession(QString(), false);
    m_ResponseBuffer.clear();
    // The QML caller's QString plus Qt's JSON/TLS machinery can make
    // short-lived copies that C++ cannot reliably scrub. Keep no durable copy
    // ourselves: this local object is serialized immediately, then our sole
    // retained byte buffer is wiped after QSslSocket accepts it.
    QJsonObject request{{QStringLiteral("version"), 1},
                        {QStringLiteral("op"), QStringLiteral("login")},
                        {QStringLiteral("username"), normalizedUsername},
                        {QStringLiteral("password"), password}};
    m_SecretRequest = QJsonDocument(request).toJson(QJsonDocument::Compact) + '\n';
    request.remove(QStringLiteral("password"));
    m_PendingUsername = normalizedUsername;
    auto* socket = new QSslSocket(this);
    socket->setReadBufferSize(kMaxResponseBytes + 1);
    m_Socket = socket;
    QString error;
    if (!prepareSslSocket(socket, &error)) {
        failLogin(error);
        return;
    }
    setLastError(QString());
    setAuthenticating(true);
    setStatus(QStringLiteral("Authenticating with the configured system account"));
    connect(socket, &QSslSocket::encrypted, this, [this, socket]() {
        if (socket != m_Socket || !m_Authenticating) {
            return;
        }
        const qint64 expectedBytes = m_SecretRequest.size();
        const qint64 queued = socket->write(m_SecretRequest);
        // QIODevice has copied the plaintext into its own write buffer. Clear
        // our owned request as soon as the hand-off is complete. This only
        // limits the lifetime of this buffer; it cannot promise zero copies in
        // Qt/QML or the TLS implementation. Nothing is persisted in QSettings.
        m_SecretRequest.fill('\0');
        m_SecretRequest.clear();
        if (queued != expectedBytes) {
            failLogin(QStringLiteral("Could not send system-authentication request"));
        }
    });
    connect(socket, &QSslSocket::readyRead, this, [this, socket]() { drainResponse(socket); });
    connect(socket, &QSslSocket::sslErrors, this,
            [this, socket](const QList<QSslError>& errors) {
                if (socket != m_Socket || !m_Authenticating) {
                    return;
                }
                QStringList messages;
                for (const QSslError& sslError : errors) {
                    messages.append(boundedText(sslError.errorString()));
                }
                failLogin(QStringLiteral("System-auth TLS verification failed: %1")
                              .arg(messages.join(QStringLiteral("; "))));
            });
    connect(socket, &QSslSocket::errorOccurred, this,
            [this, socket](QAbstractSocket::SocketError) {
                if (socket == m_Socket && m_Authenticating) {
                    failLogin(QStringLiteral("System-authentication connection failed"));
                }
            });
    connect(socket, &QSslSocket::disconnected, this, [this, socket]() {
        if (socket == m_Socket && m_Authenticating) {
            drainResponse(socket);
        }
        if (socket == m_Socket && m_Authenticating) {
            failLogin(QStringLiteral("System-authentication gateway disconnected before responding"));
        }
    });
    m_RequestTimeoutTimer->start();
    const QString peerName = m_ServerName.isEmpty() ? m_Host : m_ServerName;
    socket->connectToHostEncrypted(m_Host, static_cast<quint16>(m_Port), peerName);
}

void SystemAuthClient::drainResponse(QSslSocket* socket)
{
    if (socket != m_Socket || !m_Authenticating) {
        return;
    }
    m_ResponseBuffer.append(socket->readAll());
    if (m_ResponseBuffer.size() > kMaxResponseBytes) {
        failLogin(QStringLiteral("System-authentication response is too large"));
        return;
    }
    const int newline = m_ResponseBuffer.indexOf('\n');
    if (newline < 0) {
        return;
    }
    if (!m_ResponseBuffer.mid(newline + 1).isEmpty()) {
        failLogin(QStringLiteral("System-authentication response is malformed"));
        return;
    }
    finishLogin(m_ResponseBuffer.left(newline));
}

void SystemAuthClient::finishLogin(QByteArray line)
{
    QJsonParseError error;
    const QJsonDocument document = QJsonDocument::fromJson(line, &error);
    if (error.error != QJsonParseError::NoError || !document.isObject()) {
        failLogin(QStringLiteral("System-authentication response is malformed"));
        return;
    }
    const QJsonObject response = document.object();
    const QJsonValue resultValue = response.value(QStringLiteral("result"));
    if (!response.value(QStringLiteral("ok")).isBool() ||
        !response.value(QStringLiteral("ok")).toBool() || !resultValue.isObject()) {
        failLogin(QStringLiteral("System authentication failed"));
        return;
    }
    const QJsonObject result = resultValue.toObject();
    const QJsonValue ticketValue = result.value(QStringLiteral("session_token"));
    const QJsonValue subjectValue = result.value(QStringLiteral("subject"));
    const QJsonValue audienceValue = result.value(QStringLiteral("audience"));
    qint64 expiresAtUtcMs = 0;
    static const QRegularExpression ticketPattern(
        QStringLiteral("\\Aqsa1\\.[A-Za-z0-9_-]{1,1368}\\.[A-Za-z0-9_-]{43}\\z"));
    const QByteArray ticket = ticketValue.isString() ? ticketValue.toString().toLatin1() : QByteArray();
    const qint64 now = QDateTime::currentMSecsSinceEpoch();
    if (result.size() != 5 || !result.contains(QStringLiteral("version")) ||
        !result.contains(QStringLiteral("session_token")) ||
        !result.contains(QStringLiteral("subject")) ||
        !result.contains(QStringLiteral("audience")) ||
        !result.contains(QStringLiteral("expires_at_unix_ms")) ||
        result.value(QStringLiteral("version")).toInt(-1) != 1 ||
        !subjectValue.isString() || subjectValue.toString() != m_PendingUsername ||
        !audienceValue.isString() || !validAudience(audienceValue.toString()) ||
        audienceValue.toString() != m_ExpectedAudience ||
        ticket.isEmpty() || ticket.size() > 1536 ||
        !ticketPattern.match(QString::fromLatin1(ticket)).hasMatch() ||
        !exactInteger(result.value(QStringLiteral("expires_at_unix_ms")), now + 1,
                      now + kMaximumTicketLifetimeMs + kMaximumClockSkewMs,
                      &expiresAtUtcMs)) {
        line.fill('\0');
        failLogin(QStringLiteral("System-authentication response is invalid"));
        return;
    }

    line.fill('\0');
    m_ResponseBuffer.fill('\0');
    m_ResponseBuffer.clear();
    closeSocket();
    m_RequestTimeoutTimer->stop();
    setAuthenticating(false);
    m_Ticket.fill('\0');
    m_Ticket = ticket;
    m_Subject = subjectValue.toString();
    m_PendingUsername.clear();
    m_ExpiresAtUtcMs = expiresAtUtcMs;
    const qint64 remaining = m_ExpiresAtUtcMs - QDateTime::currentMSecsSinceEpoch();
    m_ExpiryTimer->start(static_cast<int>(qBound<qint64>(qint64{1}, remaining,
                                                       qint64{INT_MAX})));
    m_ClockGuardTimer->start();
    if (m_QsfClient != nullptr) {
        m_QsfClient->setEphemeralSystemAuthTicket(m_Ticket, m_ExpiresAtUtcMs);
    }
    if (m_InstallGameStreamLease) {
        m_InstallGameStreamLease(m_Host, m_Port, m_ServerName, m_CaFile,
                                 m_ExpectedAudience, m_Ticket, m_ExpiresAtUtcMs);
    }
    setLastError(QString());
    setStatus(QStringLiteral("System authentication succeeded; session is held only in memory"));
    emit authenticatedChanged();
    emit subjectChanged();
    emit expiresAtUtcChanged();
    emit sessionEstablished();
}

void SystemAuthClient::closeSocket()
{
    if (m_Socket == nullptr) {
        return;
    }
    QSslSocket* socket = m_Socket;
    m_Socket = nullptr;
    socket->disconnect(this);
    socket->abort();
    socket->deleteLater();
}

void SystemAuthClient::failLogin(const QString& error)
{
    closeSocket();
    m_RequestTimeoutTimer->stop();
    m_SecretRequest.fill('\0');
    m_SecretRequest.clear();
    m_ResponseBuffer.fill('\0');
    m_ResponseBuffer.clear();
    m_PendingUsername.clear();
    setAuthenticating(false);
    clearSession(QString(), false);
    setLastError(error);
    setStatus(QStringLiteral("System authentication failed"));
}

void SystemAuthClient::clearSession(const QString& status, bool reportStatus)
{
    const bool wasAuthenticated = !m_Ticket.isEmpty() || !m_Subject.isEmpty() || m_ExpiresAtUtcMs != 0;
    m_ExpiryTimer->stop();
    m_ClockGuardTimer->stop();
    if (m_ClearGameStreamLease) {
        m_ClearGameStreamLease();
    }
    m_Ticket.fill('\0');
    m_Ticket.clear();
    m_Subject.clear();
    m_ExpiresAtUtcMs = 0;
    if (m_QsfClient != nullptr) {
        m_QsfClient->clearEphemeralSystemAuthTicket();
    }
    if (wasAuthenticated) {
        emit authenticatedChanged();
        emit subjectChanged();
        emit expiresAtUtcChanged();
        emit sessionCleared();
    }
    if (reportStatus && !status.isEmpty()) {
        setStatus(status);
    }
}

void SystemAuthClient::expireSession()
{
    clearSession(QStringLiteral("System-authentication session expired; QSF was deactivated"), true);
}

void SystemAuthClient::logout()
{
    closeSocket();
    m_RequestTimeoutTimer->stop();
    m_SecretRequest.fill('\0');
    m_SecretRequest.clear();
    m_PendingUsername.clear();
    setAuthenticating(false);
    clearSession(QStringLiteral("Signed out; QSF was deactivated"), true);
    // Keep this distinct from clearSession() itself: expiry and a failed
    // replacement login must revoke future admission and wipe the ticket, but
    // must not tear down a media session that Sunshine has already admitted.
    emit sessionExplicitlyRevoked();
    setLastError(QString());
}

void SystemAuthClient::setAuthenticating(bool authenticating)
{
    if (m_Authenticating != authenticating) {
        m_Authenticating = authenticating;
        emit authenticatingChanged();
    }
}

void SystemAuthClient::setStatus(const QString& status)
{
    if (m_Status != status) {
        m_Status = status;
        emit statusChanged();
    }
}

void SystemAuthClient::setLastError(const QString& error)
{
    if (m_LastError != error) {
        m_LastError = error;
        emit lastErrorChanged();
    }
}
