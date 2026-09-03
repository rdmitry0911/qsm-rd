// SPDX-License-Identifier: GPL-3.0-or-later
// Client-side implementation of the q-sunshine QSF mTLS companion protocol.

#pragma once

#include <QByteArray>
#include <QJsonObject>
#include <QObject>
#include <QQueue>
#include <QString>

class QClipboard;
class ProfileNegotiationCoordinator;
class QSslSocket;
class QTimer;
class QsfClientTestAccess;

class QsfClient final : public QObject
{
    Q_OBJECT

    Q_PROPERTY(QString profileId READ profileId NOTIFY profileChanged)
    Q_PROPERTY(QString host READ host NOTIFY configurationChanged)
    Q_PROPERTY(int port READ port NOTIFY configurationChanged)
    Q_PROPERTY(QString serverName READ serverName NOTIFY configurationChanged)
    Q_PROPERTY(QString caFile READ caFile NOTIFY configurationChanged)
    Q_PROPERTY(QString clientCertificateFile READ clientCertificateFile NOTIFY configurationChanged)
    Q_PROPERTY(QString clientKeyFile READ clientKeyFile NOTIFY configurationChanged)
    Q_PROPERTY(bool configured READ configured NOTIFY configurationChanged)
    Q_PROPERTY(bool sessionActive READ sessionActive WRITE setSessionActive
               NOTIFY sessionActiveChanged)
    Q_PROPERTY(bool ready READ ready NOTIFY readyChanged)
    Q_PROPERTY(bool clipboardSyncEnabled READ clipboardSyncEnabled
               WRITE setClipboardSyncEnabled NOTIFY clipboardSyncEnabledChanged)
    Q_PROPERTY(QString initialClipboardDirection READ initialClipboardDirection
               WRITE setInitialClipboardDirection NOTIFY initialClipboardDirectionChanged)
    Q_PROPERTY(QString status READ status NOTIFY statusChanged)
    Q_PROPERTY(QString lastError READ lastError NOTIFY lastErrorChanged)
    Q_PROPERTY(QString lastResult READ lastResult NOTIFY lastResultChanged)

public:
    explicit QsfClient(QObject* parent = nullptr);

    QString profileId() const;
    QString host() const;
    int port() const;
    QString serverName() const;
    QString caFile() const;
    QString clientCertificateFile() const;
    QString clientKeyFile() const;
    bool configured() const;
    bool sessionActive() const;
    bool ready() const;
    bool clipboardSyncEnabled() const;
    QString initialClipboardDirection() const;
    QString status() const;
    QString lastError() const;
    QString lastResult() const;

    Q_INVOKABLE void selectProfile(const QString& profileId);
    Q_INVOKABLE bool applyConfiguration(const QString& host,
                                        int port,
                                        const QString& serverName,
                                        const QString& caFile,
                                        const QString& clientCertificateFile,
                                        const QString& clientKeyFile);
    Q_INVOKABLE bool applyConfigurationText(const QString& host,
                                            const QString& port,
                                            const QString& serverName,
                                            const QString& caFile,
                                            const QString& clientCertificateFile,
                                            const QString& clientKeyFile);
    Q_INVOKABLE void testConnection();
    Q_INVOKABLE void requestResize(int width, int height);
    Q_INVOKABLE void requestResizeText(const QString& resolution);
    Q_INVOKABLE void uploadFile(const QString& sourcePath, const QString& guestName);
    Q_INVOKABLE void downloadFile(const QString& guestName, const QString& destinationPath);

    void setSessionActive(bool active);
    void setClipboardSyncEnabled(bool enabled);
    void setInitialClipboardDirection(const QString& direction);
    // This C++-only API is intentionally not Q_INVOKABLE and never stores a
    // ticket in QSettings. SystemAuthClient owns the ticket lifecycle and
    // calls this after a successful TLS/PAM login. A legacy mTLS profile keeps
    // using its configured certificate/key files unchanged.
    void setEphemeralSystemAuthTicket(const QByteArray& ticket, qint64 expiresAtUtcMs);
    void clearEphemeralSystemAuthTicket();
    bool hasValidEphemeralSystemAuthTicket() const;

signals:
    void profileChanged();
    void configurationChanged();
    void sessionActiveChanged();
    void readyChanged();
    void clipboardSyncEnabledChanged();
    void initialClipboardDirectionChanged();
    void statusChanged();
    void lastErrorChanged();
    void lastResultChanged();
    // Deliberately payload-free: consumers can observe completion without
    // copying sensitive clipboard contents into logs or QML state.
    void clipboardReceivedFromGuest();
    void clipboardSentToGuest();
    void resizeApplied(int width, int height, bool qemuApplied);
    // Direct slots run synchronously here, before the encrypted request bytes
    // enter QSslSocket. The coordinator persists its crash-recovery guard at
    // this exact admission boundary.
    void connectionProfileRequestAboutToDispatch();
    // Emitted only after the profile request bytes have been accepted by the
    // encrypted socket.  From this point the gateway may already be carrying
    // out SetUIInfo/guest work even if the desktop later loses its transport.
    void connectionProfileRequestDispatched();
    void connectionProfileReceived(int width, int height, int fps, int bitrateKbps,
                                   const QString& videoCodec, bool qemuApplied);
    void fileTransferFinished(QString description);

private:
    friend class ProfileNegotiationCoordinator;
    friend class QsfClientTestAccess;
    friend class SystemAuthClient;

    struct Request {
        QString operation;
        QJsonObject payload;
        QString context;
        bool requiresActiveSession = true;
        bool requiresReadySession = true;
        quint64 activationEpoch = 0;
        QByteArray clipboardRevision;
    };

    QString settingsGroup() const;
    void loadProfile();
    void saveProfile() const;
    bool enqueue(const QString& operation, const QJsonObject& payload,
                 const QString& context = QString(), bool requiresActiveSession = true,
                 bool requiresReadySession = true,
                 const QByteArray& clipboardRevision = QByteArray());
    void startNextRequest();
    void drainResponse(QSslSocket* socket);
    void finishActiveRequest(const QJsonObject& response);
    void failActiveRequest(const QString& error);
    void cancelAllRequests();
    void cancelClipboardRequests();
    bool prepareSslSocket(QSslSocket* socket, QString* error) const;
    void activateSession(bool displayNegotiationOnly);
    // Only the profile coordinator can acquire this narrow lease. It is never
    // exposed as a QML action alongside normal QSF activation.
    void activateForDisplayNegotiation();
    // Likewise, only the coordinator can abort its profile-only lease while
    // the guard is live. A public sessionActive=false must not discard the
    // authoritative response after connection_optimize was dispatched.
    void deactivateForProfileHandoff();
    // Only the profile coordinator may initiate the mutating QEMU/guest
    // transaction. It is intentionally not a QML-invokable data control.
    void optimizeConnectionForDisplay(const QString& requestedResolution,
                                      const QString& decoderPreference);
    // Kept in lockstep with ProfileNegotiationCoordinator::busy(). It blocks
    // normal QSF activation through the writable QML property while a remote
    // scanout transaction or durable restart-recovery guard owns the session.
    // Its mutator remains coordinator-private so another C++ caller cannot
    // release the guard behind an in-flight remote profile transaction.
    void setProfileHandoffOperationsBlocked(bool blocked);
    // Public QSF controls must not replace/cancel the coordinator's narrow
    // profile-only lease or mutate its endpoint while a guest transaction is
    // live. Keep expected refusals off lastError for the same reason as the
    // Moonlight controller guard.
    bool profileHandoffBlocksPublicOperation(const QString& operation);
    void deactivateSession();
    bool canOperateSession(QString* error,
                           bool allowDisplayNegotiationOnly = false) const;
    void setStatus(const QString& status);
    void setLastError(const QString& error);
    void setLastResult(const QString& result);
    void setReady(bool ready);
    void updateClipboardPolling();
    void startClipboardSynchronization();
    void handleClipboardChanged();
    void queueClipboardGet();
    void queueClipboardSet(const QString& text);
    void scheduleClipboardRetry();
    void resetClipboardRetry();
    void clearPendingClipboard();
    // Called only by SystemAuthClient after a terminal broker has
    // authenticated the selected VM and supplied an authoritative QSF route.
    // It is deliberately not Q_INVOKABLE: a QML caller must not be able to
    // select the server that receives an in-memory VM ticket.
    bool applyBrokerConfiguration(const QString& host, int port,
                                  const QString& serverName, const QString& caFile);
    bool handleClipboardReply(const QJsonObject& result,
                              const QByteArray& requestLocalRevision);
    void enqueueLatestResizeIfNeeded();
    bool hasQueuedOperation(const QString& operation) const;

    static bool validateClipboard(const QString& text, QString* error);
    static bool validFileName(const QString& name);
    static QByteArray clipboardHash(const QString& text);

    QString m_ProfileId;
    QString m_Host;
    int m_Port;
    QString m_ServerName;
    QString m_CaFile;
    QString m_ClientCertificateFile;
    QString m_ClientKeyFile;
    QByteArray m_EphemeralSystemAuthTicket;
    qint64 m_EphemeralSystemAuthTicketExpiresAtUtcMs;
    bool m_UseEphemeralSystemAuthTicket;
    bool m_SessionActive;
    bool m_DisplayNegotiationOnly;
    bool m_ProfileHandoffOperationsBlocked;
    bool m_Ready;
    bool m_ClipboardSyncEnabled;
    QString m_InitialClipboardDirection;
    QString m_Status;
    QString m_LastError;
    QString m_LastResult;
    QQueue<Request> m_Queue;
    Request m_ActiveRequest;
    bool m_HasActiveRequest;
    QSslSocket* m_Socket;
    QByteArray m_ResponseBuffer;
    QClipboard* m_Clipboard;
    QTimer* m_ClipboardPollTimer;
    QTimer* m_ClipboardRetryTimer;
    QTimer* m_RequestTimeoutTimer;
    bool m_ClipboardGetQueued;
    bool m_ClipboardSetInFlight;
    bool m_ClipboardSetQueued;
    bool m_HasPendingLocalClipboard;
    QString m_PendingClipboardText;
    QByteArray m_PendingClipboardRevision;
    QByteArray m_RemoteClipboardHash;
    int m_ClipboardRetryAttempt;
    quint64 m_ClipboardRetryEpoch;
    int m_DesiredResizeWidth;
    int m_DesiredResizeHeight;
    int m_LastResizeWidth;
    int m_LastResizeHeight;
    bool m_ResizeInFlight;
    quint64 m_ActivationEpoch;
};
