// SPDX-License-Identifier: GPL-3.0-or-later

#include "systemauthclient.h"

#include "qsfclient.h"

#include <QAbstractSocket>
#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QHostAddress>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonParseError>
#include <QRegularExpression>
#include <QSettings>
#include <QSslCertificate>
#include <QSslConfiguration>
#include <QSslSocket>
#include <QTemporaryFile>
#include <QTimer>

#include <climits>
#include <cmath>

#ifdef Q_OS_UNIX
#include <cerrno>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace {

constexpr int kMaxResponseBytes = 8 * 1024;
constexpr int kRequestTimeoutMs = 15000;
constexpr int kClockGuardIntervalMs = 1000;
constexpr qint64 kMaximumTicketLifetimeMs = 15 * 60 * 1000;
constexpr qint64 kMaximumClockSkewMs = 30 * 1000;
constexpr qint64 kMaximumLaunchDescriptorLifetimeMs = 5 * 60 * 1000;
constexpr qint64 kMaxLaunchDescriptorBytes = 96 * 1024;
constexpr qint64 kMaxLaunchCaBytes = 64 * 1024;

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

bool validRouteHost(const QString& host)
{
    const QString value = host.trimmed();
    if (value.isEmpty() || value.size() > 253) {
        return false;
    }
    for (const QChar character : value) {
        if (character.isNull() || character.category() == QChar::Other_Control ||
            character.isSpace()) {
            return false;
        }
    }
    QHostAddress address;
    if (address.setAddress(value)) {
        return true;
    }
    // The broker returns a bare DNS name, never a URI, port, userinfo, or a
    // wildcard. Restrict this to ASCII/punycode DNS syntax before it reaches
    // Moonlight or QSslSocket.
    static const QRegularExpression dnsName(
        QStringLiteral("\\A(?=.{1,253}\\z)(?!.*\\.\\.)[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\\z"));
    return dnsName.match(value).hasMatch();
}

struct BrokerRoutes {
    QString mediaHost;
    int mediaPort = 0;
    QString qsfHost;
    int qsfPort = 0;
    QString leaseHost;
    int leasePort = 0;
};

bool parseBrokerRoute(const QJsonObject& object, QString* host, int* port)
{
    qint64 value = 0;
    if (object.size() != 2 || !object.contains(QStringLiteral("host")) ||
        !object.contains(QStringLiteral("port")) || !object.value(QStringLiteral("host")).isString() ||
        !exactInteger(object.value(QStringLiteral("port")), 1, 65535, &value)) {
        return false;
    }
    const QString normalizedHost = object.value(QStringLiteral("host")).toString().trimmed();
    if (!validRouteHost(normalizedHost)) {
        return false;
    }
    *host = normalizedHost;
    *port = static_cast<int>(value);
    return true;
}

bool parseBrokerRoutes(const QJsonValue& value, BrokerRoutes* routes)
{
    if (routes == nullptr || !value.isObject()) {
        return false;
    }
    const QJsonObject object = value.toObject();
    if (object.size() != 3 || !object.contains(QStringLiteral("media")) ||
        !object.contains(QStringLiteral("qsf")) || !object.contains(QStringLiteral("lease")) ||
        !object.value(QStringLiteral("media")).isObject() ||
        !object.value(QStringLiteral("qsf")).isObject() ||
        !object.value(QStringLiteral("lease")).isObject() ||
        !parseBrokerRoute(object.value(QStringLiteral("media")).toObject(),
                          &routes->mediaHost, &routes->mediaPort) ||
        !parseBrokerRoute(object.value(QStringLiteral("qsf")).toObject(),
                          &routes->qsfHost, &routes->qsfPort) ||
        !parseBrokerRoute(object.value(QStringLiteral("lease")).toObject(),
                          &routes->leaseHost, &routes->leasePort) ||
        routes->mediaPort <= 5) {
        return false;
    }
    return true;
}

struct LaunchDescriptor {
    QString host;
    int port = 0;
    QString serverName;
    QByteArray caPem;
    QByteArray claim;
    qint64 expiresAtUtcMs = 0;
};

bool hasExactKeys(const QJsonObject& object, const QStringList& keys)
{
    if (object.size() != keys.size()) {
        return false;
    }
    for (const QString& key : keys) {
        if (!object.contains(key)) {
            return false;
        }
    }
    return true;
}

bool readOwnerPrivateLaunchFile(const QString& path, QByteArray* contents)
{
    if (contents == nullptr || !QFileInfo(path).isAbsolute()) {
        return false;
    }
#ifdef Q_OS_UNIX
    const QByteArray encodedPath = QFile::encodeName(path);
    const int descriptorFd = ::open(encodedPath.constData(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (descriptorFd < 0) {
        return false;
    }
    struct stat before {};
    const bool validBefore = ::fstat(descriptorFd, &before) == 0 && S_ISREG(before.st_mode) &&
        before.st_uid == ::geteuid() && (before.st_mode & (S_IWGRP | S_IWOTH)) == 0 &&
        before.st_size > 0 && before.st_size <= kMaxLaunchDescriptorBytes;
    if (!validBefore) {
        ::close(descriptorFd);
        return false;
    }
    QByteArray bytes(static_cast<int>(before.st_size), Qt::Uninitialized);
    qsizetype offset = 0;
    bool readOk = true;
    while (offset < bytes.size()) {
        const ssize_t readCount = ::read(descriptorFd, bytes.data() + offset,
                                         static_cast<size_t>(bytes.size() - offset));
        if (readCount > 0) {
            offset += readCount;
            continue;
        }
        if (readCount < 0 && errno == EINTR) {
            continue;
        }
        readOk = false;
        break;
    }
    char extra = '\0';
    if (readOk) {
        const ssize_t extraRead = ::read(descriptorFd, &extra, 1);
        readOk = extraRead == 0;
    }
    struct stat after {};
    const bool sameFile = ::fstat(descriptorFd, &after) == 0 && before.st_dev == after.st_dev &&
        before.st_ino == after.st_ino && before.st_size == after.st_size;
    ::close(descriptorFd);
    if (!readOk || !sameFile) {
        bytes.fill('\0');
        return false;
    }
    *contents = std::move(bytes);
    return true;
#else
    QFileInfo info(path);
    if (!info.isFile() || info.isSymLink() || info.size() <= 0 ||
        info.size() > kMaxLaunchDescriptorBytes) {
        return false;
    }
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        return false;
    }
    const QByteArray bytes = file.read(kMaxLaunchDescriptorBytes + 1);
    if (bytes.isEmpty() || bytes.size() > kMaxLaunchDescriptorBytes) {
        return false;
    }
    *contents = bytes;
    return true;
#endif
}

bool parseLaunchDescriptor(QByteArray* bytes, LaunchDescriptor* descriptor)
{
    if (bytes == nullptr || descriptor == nullptr) {
        return false;
    }
    QJsonParseError parseError;
    const QJsonDocument document = QJsonDocument::fromJson(*bytes, &parseError);
    bytes->fill('\0');
    bytes->clear();
    if (parseError.error != QJsonParseError::NoError || !document.isObject()) {
        return false;
    }
    const QJsonObject root = document.object();
    const QStringList rootKeys {QStringLiteral("version"), QStringLiteral("kind"),
                                QStringLiteral("endpoint"), QStringLiteral("claim"),
                                QStringLiteral("expires_at_utc_ms")};
    if (!hasExactKeys(root, rootKeys) || !root.value(QStringLiteral("endpoint")).isObject() ||
        !root.value(QStringLiteral("kind")).isString() || !root.value(QStringLiteral("claim")).isString()) {
        return false;
    }
    qint64 version = 0;
    qint64 endpointPort = 0;
    qint64 expiresAtUtcMs = 0;
    const qint64 now = QDateTime::currentMSecsSinceEpoch();
    const QJsonObject endpoint = root.value(QStringLiteral("endpoint")).toObject();
    const QStringList endpointKeys {QStringLiteral("host"), QStringLiteral("port"),
                                    QStringLiteral("server_name"), QStringLiteral("ca_pem")};
    if (!hasExactKeys(endpoint, endpointKeys) || !endpoint.value(QStringLiteral("host")).isString() ||
        !endpoint.value(QStringLiteral("server_name")).isString() ||
        !endpoint.value(QStringLiteral("ca_pem")).isString() ||
        !exactInteger(root.value(QStringLiteral("version")), 1, 1, &version) ||
        !exactInteger(endpoint.value(QStringLiteral("port")), 1, 65535, &endpointPort) ||
        !exactInteger(root.value(QStringLiteral("expires_at_utc_ms")), now + 1,
                      now + kMaximumLaunchDescriptorLifetimeMs + kMaximumClockSkewMs,
                      &expiresAtUtcMs) ||
        root.value(QStringLiteral("kind")).toString() != QStringLiteral("q-sunshine-pve-launch")) {
        return false;
    }
    const QString host = endpoint.value(QStringLiteral("host")).toString().trimmed();
    const QString serverName = endpoint.value(QStringLiteral("server_name")).toString().trimmed();
    const QByteArray caPem = endpoint.value(QStringLiteral("ca_pem")).toString().toUtf8();
    const QByteArray claim = root.value(QStringLiteral("claim")).toString().toLatin1();
    static const QRegularExpression claimPattern(
        QStringLiteral("\\Aqsd1\\.[A-Za-z0-9_-]{43}\\z"));
    if (!validRouteHost(host) || (!serverName.isEmpty() && !validRouteHost(serverName)) ||
        caPem.isEmpty() || caPem.size() > kMaxLaunchCaBytes || caPem.contains("PRIVATE KEY") ||
        QSslCertificate::fromData(caPem, QSsl::Pem).isEmpty() ||
        !claimPattern.match(QString::fromLatin1(claim)).hasMatch()) {
        return false;
    }
    descriptor->host = host;
    descriptor->port = static_cast<int>(endpointPort);
    descriptor->serverName = serverName;
    descriptor->caPem = caPem;
    descriptor->claim = claim;
    descriptor->expiresAtUtcMs = expiresAtUtcMs;
    return true;
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
      m_QsfClient(nullptr),
      m_ApplyingBrokerRoutes(false),
      m_LaunchDescriptorMode(false),
      m_LaunchPort(0),
      m_LaunchDescriptorExpiresAtUtcMs(0)
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

SystemAuthClient::~SystemAuthClient()
{
    clearBrokerQsfTrust();
    clearLaunchDescriptor();
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
    // An empty custom CA is deliberate: QSslConfiguration's default system
    // trust store still performs strict peer verification.  Deployments with
    // a private CA provide its PEM through Advanced settings; there is never
    // a self-signed or ignore-errors fallback.
    return !m_Host.isEmpty() && m_Port >= 1 && m_Port <= 65535 &&
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
        !validAudience(normalizedAudience)) {
        setLastError(QStringLiteral("System-auth endpoint requires a host, port in 1..65535, and expected VM audience"));
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
        if (m_ApplyingBrokerRoutes || m_QsfClient == nullptr ||
            m_QsfClient->hasValidEphemeralSystemAuthTicket()) {
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

void SystemAuthClient::setBrokerMediaRouteSink(BrokerMediaRouteSink installRoute,
                                               std::function<void()> clearRoute)
{
    if (m_ClearBrokerMediaRoute) {
        m_ClearBrokerMediaRoute();
    }
    m_InstallBrokerMediaRoute = std::move(installRoute);
    m_ClearBrokerMediaRoute = std::move(clearRoute);
}

void SystemAuthClient::setBrokerGameStreamLeaseSink(BrokerGameStreamLeaseSink installLease)
{
    m_InstallBrokerGameStreamLease = std::move(installLease);
}

bool SystemAuthClient::prepareSslSocket(QSslSocket* socket, QString* error) const
{
    QSslConfiguration configuration = QSslConfiguration::defaultConfiguration();
    configuration.setProtocol(QSsl::TlsV1_3OrLater);
    const bool launchDescriptor = m_LaunchDescriptorMode;
    if (launchDescriptor) {
        const QList<QSslCertificate> certificates =
            QSslCertificate::fromData(m_LaunchCaPem, QSsl::Pem);
        if (m_LaunchHost.isEmpty() || m_LaunchPort < 1 || m_LaunchPort > 65535 ||
            certificates.isEmpty()) {
            *error = QStringLiteral("Launch descriptor trust configuration is invalid");
            return false;
        }
        configuration.setCaCertificates(certificates);
    }
    else if (!m_CaFile.isEmpty()) {
        QFile file(m_CaFile);
        if (!file.open(QIODevice::ReadOnly)) {
            *error = QStringLiteral("Unable to read system-auth CA certificate file");
            return false;
        }
        const QList<QSslCertificate> certificates =
            QSslCertificate::fromData(file.readAll(), QSsl::Pem);
        if (certificates.isEmpty()) {
            *error = QStringLiteral("Invalid system-auth CA certificate file");
            return false;
        }
        configuration.setCaCertificates(certificates);
    }
    socket->setSslConfiguration(configuration);
    socket->setPeerVerifyMode(QSslSocket::VerifyPeer);
    socket->setPeerVerifyName(launchDescriptor
                                  ? (m_LaunchServerName.isEmpty() ? m_LaunchHost : m_LaunchServerName)
                                  : (m_ServerName.isEmpty() ? m_Host : m_ServerName));
    return true;
}

void SystemAuthClient::clearLaunchDescriptor()
{
    m_LaunchDescriptorMode = false;
    m_LaunchHost.clear();
    m_LaunchPort = 0;
    m_LaunchServerName.clear();
    m_LaunchCaPem.fill('\0');
    m_LaunchCaPem.clear();
    m_LaunchDescriptorExpiresAtUtcMs = 0;
}

bool SystemAuthClient::installBrokerQsfTrust(const QByteArray& caPem, QString* path,
                                             QString* error)
{
    if (path == nullptr || error == nullptr || caPem.isEmpty() || caPem.size() > kMaxLaunchCaBytes ||
        caPem.contains("PRIVATE KEY") || QSslCertificate::fromData(caPem, QSsl::Pem).isEmpty()) {
        if (error != nullptr) {
            *error = QStringLiteral("Broker returned invalid transport trust material");
        }
        return false;
    }
    clearBrokerQsfTrust();
    auto file = std::make_unique<QTemporaryFile>(
        QDir::tempPath() + QStringLiteral("/q-sunshine-qsf-ca-XXXXXX.pem"));
    file->setAutoRemove(true);
    if (!file->open()) {
        *error = QStringLiteral("Could not create ephemeral QSF trust file");
        return false;
    }
    if (!file->setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner) ||
        file->write(caPem) != caPem.size() || !file->flush()) {
        file->close();
        file->remove();
        *error = QStringLiteral("Could not write ephemeral QSF trust file");
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
        *error = QStringLiteral("Ephemeral QSF trust file permissions are unsafe");
        return false;
    }
    *path = filePath;
    m_BrokerQsfCaFile = std::move(file);
    return true;
}

void SystemAuthClient::clearBrokerQsfTrust()
{
    if (!m_BrokerQsfCaFile) {
        return;
    }
    QTemporaryFile* file = m_BrokerQsfCaFile.get();
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
    m_BrokerQsfCaFile.reset();
}

bool SystemAuthClient::startClaimSocket(QString* error)
{
    if (error == nullptr || !m_LaunchDescriptorMode || m_LaunchHost.isEmpty() ||
        m_LaunchPort < 1 || m_LaunchPort > 65535 ||
        m_LaunchDescriptorExpiresAtUtcMs <= QDateTime::currentMSecsSinceEpoch()) {
        if (error != nullptr) {
            *error = QStringLiteral("Launch descriptor is expired or invalid");
        }
        return false;
    }
    auto* socket = new QSslSocket(this);
    socket->setReadBufferSize(kMaxResponseBytes + 1);
    m_Socket = socket;
    if (!prepareSslSocket(socket, error)) {
        return false;
    }
    setLastError(QString());
    setAuthenticating(true);
    setStatus(QStringLiteral("Redeeming one-use Proxmox launch authorization"));
    connect(socket, &QSslSocket::encrypted, this, [this, socket]() {
        if (socket != m_Socket || !m_Authenticating) {
            return;
        }
        if (m_LaunchDescriptorExpiresAtUtcMs <= QDateTime::currentMSecsSinceEpoch()) {
            failLogin(QStringLiteral("Launch authorization expired before it could be redeemed"));
            return;
        }
        const qint64 expectedBytes = m_SecretRequest.size();
        const qint64 queued = socket->write(m_SecretRequest);
        m_SecretRequest.fill('\0');
        m_SecretRequest.clear();
        if (queued != expectedBytes) {
            failLogin(QStringLiteral("Could not send launch authorization claim"));
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
                failLogin(QStringLiteral("Launch endpoint TLS verification failed: %1")
                              .arg(messages.join(QStringLiteral("; "))));
            });
    connect(socket, &QSslSocket::errorOccurred, this,
            [this, socket](QAbstractSocket::SocketError) {
                if (socket == m_Socket && m_Authenticating) {
                    failLogin(QStringLiteral("Launch endpoint connection failed"));
                }
            });
    connect(socket, &QSslSocket::disconnected, this, [this, socket]() {
        if (socket == m_Socket && m_Authenticating) {
            drainResponse(socket);
        }
        if (socket == m_Socket && m_Authenticating) {
            failLogin(QStringLiteral("Launch endpoint disconnected before responding"));
        }
    });
    m_RequestTimeoutTimer->start();
    const QString peerName = m_LaunchServerName.isEmpty() ? m_LaunchHost : m_LaunchServerName;
    socket->connectToHostEncrypted(m_LaunchHost, static_cast<quint16>(m_LaunchPort), peerName);
    return true;
}

bool SystemAuthClient::claimLaunchFile(const QString& path)
{
    if (m_Authenticating) {
        setLastError(QStringLiteral("A launch authorization claim is already in progress"));
        return false;
    }
    QByteArray bytes;
    LaunchDescriptor descriptor;
    if (!readOwnerPrivateLaunchFile(path, &bytes) || !parseLaunchDescriptor(&bytes, &descriptor)) {
        bytes.fill('\0');
        setLastError(QStringLiteral("Launch file is malformed, expired, or not owner-private"));
        setStatus(QStringLiteral("Launch authorization was rejected before connecting"));
        return false;
    }
    if (descriptor.expiresAtUtcMs <= QDateTime::currentMSecsSinceEpoch()) {
        descriptor.caPem.fill('\0');
        descriptor.claim.fill('\0');
        setLastError(QStringLiteral("Launch authorization has expired"));
        setStatus(QStringLiteral("Launch authorization was rejected before connecting"));
        return false;
    }

    // A new one-use descriptor is a fresh VM authorization boundary. End any
    // prior local admission before retaining the new broker endpoint/claim.
    closeSocket();
    m_RequestTimeoutTimer->stop();
    m_SecretRequest.fill('\0');
    m_SecretRequest.clear();
    m_ResponseBuffer.fill('\0');
    m_ResponseBuffer.clear();
    m_PendingUsername.clear();
    setAuthenticating(false);
    clearSession(QString(), false);
    clearLaunchDescriptor();
    m_LaunchDescriptorMode = true;
    m_LaunchHost = descriptor.host;
    m_LaunchPort = descriptor.port;
    m_LaunchServerName = descriptor.serverName;
    m_LaunchCaPem = descriptor.caPem;
    m_LaunchDescriptorExpiresAtUtcMs = descriptor.expiresAtUtcMs;
    QJsonObject request{{QStringLiteral("version"), 1},
                        {QStringLiteral("op"), QStringLiteral("redeem_launch")},
                        {QStringLiteral("claim"), QString::fromLatin1(descriptor.claim)}};
    descriptor.claim.fill('\0');
    descriptor.caPem.fill('\0');
    m_SecretRequest = QJsonDocument(request).toJson(QJsonDocument::Compact) + '\n';
    request.remove(QStringLiteral("claim"));
    QString error;
    if (!startClaimSocket(&error)) {
        failLogin(error);
        return false;
    }
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
    if (m_LaunchDescriptorMode) {
        finishLaunchClaim(std::move(line));
        return;
    }
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

void SystemAuthClient::finishLaunchClaim(QByteArray line)
{
    QJsonParseError parseError;
    const QJsonDocument document = QJsonDocument::fromJson(line, &parseError);
    if (parseError.error != QJsonParseError::NoError || !document.isObject()) {
        line.fill('\0');
        failLogin(QStringLiteral("Launch endpoint response is malformed"));
        return;
    }
    const QJsonObject response = document.object();
    const QJsonValue resultValue = response.value(QStringLiteral("result"));
    if (!hasExactKeys(response, {QStringLiteral("ok"), QStringLiteral("result")}) ||
        !response.value(QStringLiteral("ok")).isBool() || !response.value(QStringLiteral("ok")).toBool() ||
        !resultValue.isObject()) {
        line.fill('\0');
        failLogin(QStringLiteral("Launch authorization was rejected"));
        return;
    }
    const QJsonObject result = resultValue.toObject();
    const QStringList resultKeys {QStringLiteral("version"), QStringLiteral("session_token"),
                                  QStringLiteral("subject"), QStringLiteral("audience"),
                                  QStringLiteral("expires_at_utc_ms"), QStringLiteral("routes"),
                                  QStringLiteral("server_name"), QStringLiteral("ca_pem")};
    const QJsonValue ticketValue = result.value(QStringLiteral("session_token"));
    const QJsonValue subjectValue = result.value(QStringLiteral("subject"));
    const QJsonValue audienceValue = result.value(QStringLiteral("audience"));
    const QByteArray ticket = ticketValue.isString() ? ticketValue.toString().toLatin1() : QByteArray();
    const QByteArray caPem = result.value(QStringLiteral("ca_pem")).isString()
        ? result.value(QStringLiteral("ca_pem")).toString().toUtf8() : QByteArray();
    const QString serverName = result.value(QStringLiteral("server_name")).isString()
        ? result.value(QStringLiteral("server_name")).toString().trimmed() : QString();
    const qint64 now = QDateTime::currentMSecsSinceEpoch();
    qint64 version = 0;
    qint64 expiresAtUtcMs = 0;
    BrokerRoutes routes;
    static const QRegularExpression ticketPattern(
        QStringLiteral("\\Aqsa1\\.[A-Za-z0-9_-]{1,1368}\\.[A-Za-z0-9_-]{43}\\z"));
    const bool valid = hasExactKeys(result, resultKeys) &&
        exactInteger(result.value(QStringLiteral("version")), 1, 1, &version) &&
        subjectValue.isString() && validUsername(subjectValue.toString()) &&
        audienceValue.isString() && validAudience(audienceValue.toString()) &&
        exactInteger(result.value(QStringLiteral("expires_at_utc_ms")), now + 1,
                     now + kMaximumTicketLifetimeMs + kMaximumClockSkewMs, &expiresAtUtcMs) &&
        parseBrokerRoutes(result.value(QStringLiteral("routes")), &routes) &&
        (serverName.isEmpty() || validRouteHost(serverName)) &&
        !caPem.isEmpty() && caPem.size() <= kMaxLaunchCaBytes && !caPem.contains("PRIVATE KEY") &&
        !QSslCertificate::fromData(caPem, QSsl::Pem).isEmpty() &&
        !ticket.isEmpty() && ticket.size() <= 1536 &&
        ticketPattern.match(QString::fromLatin1(ticket)).hasMatch() &&
        m_LaunchDescriptorExpiresAtUtcMs > now;
    if (!valid || m_QsfClient == nullptr || !m_InstallBrokerMediaRoute ||
        !m_InstallBrokerGameStreamLease) {
        line.fill('\0');
        failLogin(QStringLiteral("Launch endpoint response is invalid"));
        return;
    }

    QString qsfCaPath;
    QString trustError;
    if (!installBrokerQsfTrust(caPem, &qsfCaPath, &trustError)) {
        line.fill('\0');
        failLogin(trustError);
        return;
    }
    m_ApplyingBrokerRoutes = true;
    const bool qsfConfigured = m_QsfClient->applyBrokerConfiguration(
        routes.qsfHost, routes.qsfPort, serverName, qsfCaPath);
    m_ApplyingBrokerRoutes = false;
    if (!qsfConfigured || !m_InstallBrokerMediaRoute(routes.mediaHost, routes.mediaPort) ||
        !m_InstallBrokerGameStreamLease(routes.leaseHost, routes.leasePort, serverName, caPem,
                                        audienceValue.toString(), ticket, expiresAtUtcMs)) {
        line.fill('\0');
        clearBrokerQsfTrust();
        failLogin(QStringLiteral("Launch endpoint returned an unusable VM route"));
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
    m_ExpiresAtUtcMs = expiresAtUtcMs;
    const qint64 remaining = m_ExpiresAtUtcMs - QDateTime::currentMSecsSinceEpoch();
    m_ExpiryTimer->start(static_cast<int>(qBound<qint64>(qint64{1}, remaining,
                                                       qint64{INT_MAX})));
    m_ClockGuardTimer->start();
    m_QsfClient->setEphemeralSystemAuthTicket(m_Ticket, m_ExpiresAtUtcMs);
    clearLaunchDescriptor();
    setLastError(QString());
    setStatus(QStringLiteral("Proxmox launch authorization redeemed; VM transport is ready"));
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
    if (m_ClearBrokerMediaRoute) {
        m_ClearBrokerMediaRoute();
    }
    m_Ticket.fill('\0');
    m_Ticket.clear();
    m_Subject.clear();
    m_ExpiresAtUtcMs = 0;
    if (m_QsfClient != nullptr) {
        m_QsfClient->clearEphemeralSystemAuthTicket();
    }
    clearBrokerQsfTrust();
    clearLaunchDescriptor();
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
