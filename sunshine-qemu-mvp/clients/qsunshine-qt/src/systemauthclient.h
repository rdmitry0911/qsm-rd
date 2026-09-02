// SPDX-License-Identifier: GPL-3.0-or-later
// TLS/PAM system-auth client. It holds only a short-lived in-memory ticket.

#pragma once

#include <QByteArray>
#include <QDateTime>
#include <QObject>
#include <QString>

#include <functional>

class QSslSocket;
class QTimer;
class QsfClient;
class SystemAuthClientTestAccess;

class SystemAuthClient final : public QObject
{
    Q_OBJECT

    Q_PROPERTY(QString profileId READ profileId NOTIFY profileChanged)
    Q_PROPERTY(QString host READ host NOTIFY configurationChanged)
    Q_PROPERTY(int port READ port NOTIFY configurationChanged)
    Q_PROPERTY(QString serverName READ serverName NOTIFY configurationChanged)
    Q_PROPERTY(QString caFile READ caFile NOTIFY configurationChanged)
    Q_PROPERTY(QString expectedAudience READ expectedAudience NOTIFY configurationChanged)
    Q_PROPERTY(bool configured READ configured NOTIFY configurationChanged)
    Q_PROPERTY(bool authenticating READ authenticating NOTIFY authenticatingChanged)
    Q_PROPERTY(bool authenticated READ authenticated NOTIFY authenticatedChanged)
    Q_PROPERTY(QString subject READ subject NOTIFY subjectChanged)
    Q_PROPERTY(QDateTime expiresAtUtc READ expiresAtUtc NOTIFY expiresAtUtcChanged)
    Q_PROPERTY(QString status READ status NOTIFY statusChanged)
    Q_PROPERTY(QString lastError READ lastError NOTIFY lastErrorChanged)

public:
    explicit SystemAuthClient(QObject* parent = nullptr);

    QString profileId() const;
    QString host() const;
    int port() const;
    QString serverName() const;
    QString caFile() const;
    QString expectedAudience() const;
    bool configured() const;
    bool authenticating() const;
    bool authenticated() const;
    QString subject() const;
    QDateTime expiresAtUtc() const;
    QString status() const;
    QString lastError() const;

    // Only host/SNI/CA/audience routing metadata is persisted per desktop
    // profile. Passwords, bearer tickets, subjects, and expiry timestamps are
    // deliberately not.
    Q_INVOKABLE bool selectProfile(const QString& profileId);
    Q_INVOKABLE bool applyConfiguration(const QString& host, int port,
                                        const QString& serverName,
                                        const QString& caFile,
                                        const QString& expectedAudience);
    Q_INVOKABLE bool applyConfigurationText(const QString& host, const QString& port,
                                            const QString& serverName,
                                            const QString& caFile,
                                            const QString& expectedAudience);
    Q_INVOKABLE void login(const QString& username, const QString& password);
    Q_INVOKABLE void logout();

    // This is a C++ composition hook, not a QML entry point. It gives the
    // QSF companion the short-lived ticket without ever putting it into a
    // Q_PROPERTY, QSettings value, path, process argument, or QML string.
    void attachQsfClient(QsfClient* client);
    // Same-process composition hook for the patched Moonlight child. The
    // callback transports the ticket only into a managed stdin pipe at child
    // launch; no signal/property ever contains the bearer value. Keeping a
    // narrow callback avoids making the standalone auth protocol test link a
    // GUI/process controller.
    using GameStreamLeaseSink = std::function<void(const QString&, int, const QString&,
                                                   const QString&, const QString&,
                                                   const QByteArray&, qint64)>;
    void setGameStreamLeaseSink(GameStreamLeaseSink installLease,
                                std::function<void()> clearLease);

signals:
    void profileChanged();
    void configurationChanged();
    void authenticatingChanged();
    void authenticatedChanged();
    void subjectChanged();
    void expiresAtUtcChanged();
    void statusChanged();
    void lastErrorChanged();
    // Payload-free by design. Consumers use authenticated()/subject() and do
    // not receive the bearer ticket through the signal system.
    void sessionEstablished();
    void sessionCleared();
    // Explicit sign-out (including a profile/endpoint scope revocation) is
    // different from a lease timeout.  Composition owns media teardown for
    // this event; expiry only prevents a subsequent launch, because an
    // already established GameStream session has its own mTLS admission.
    void sessionExplicitlyRevoked();

private:
    friend class SystemAuthClientTestAccess;

    QString settingsGroup() const;
    void loadProfile();
    void saveProfile() const;
    bool prepareSslSocket(QSslSocket* socket, QString* error) const;
    void drainResponse(QSslSocket* socket);
    void finishLogin(QByteArray line);
    void failLogin(const QString& error);
    void clearSession(const QString& status, bool reportStatus);
    void expireSession();
    void closeSocket();
    void setAuthenticating(bool authenticating);
    void setStatus(const QString& status);
    void setLastError(const QString& error);
    static bool validProfileId(const QString& profileId);
    static bool validUsername(const QString& username);

    QString m_ProfileId;
    QString m_Host;
    int m_Port;
    QString m_ServerName;
    QString m_CaFile;
    QString m_ExpectedAudience;
    bool m_Authenticating;
    QString m_Subject;
    qint64 m_ExpiresAtUtcMs;
    QByteArray m_Ticket;
    QString m_PendingUsername;
    // Transient serialized login request. It is wiped after QSslSocket takes
    // it or on every failure/logout path. Qt/QML can still make short-lived
    // implicit-shared copies while serializing; this is best-effort hygiene,
    // not a claim of perfect process-memory erasure.
    QByteArray m_SecretRequest;
    QString m_Status;
    QString m_LastError;
    QSslSocket* m_Socket;
    QByteArray m_ResponseBuffer;
    QTimer* m_RequestTimeoutTimer;
    QTimer* m_ExpiryTimer;
    QTimer* m_ClockGuardTimer;
    QsfClient* m_QsfClient;
    GameStreamLeaseSink m_InstallGameStreamLease;
    std::function<void()> m_ClearGameStreamLease;
};
