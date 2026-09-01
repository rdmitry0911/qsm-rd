// SPDX-License-Identifier: GPL-3.0-or-later
// Coordinates a safe display-profile handoff between Moonlight and QSF.

#pragma once

#include <QObject>
#include <QString>
#include <QtGlobal>

class MoonlightController;
class QsfClient;
class QTimer;

// A QSF profile changes the QEMU/VirGL scanout.  Sunshine's existing capture
// session is itself a scanout writer, so it must be fully retired before the
// new profile is committed.  Keeping that lifecycle in one object makes the
// UI unable to accidentally negotiate a guest mode under a live old stream.
class ProfileNegotiationCoordinator final : public QObject
{
    Q_OBJECT

    Q_PROPERTY(bool busy READ busy NOTIFY busyChanged)
    Q_PROPERTY(QString status READ status NOTIFY statusChanged)
    Q_PROPERTY(QString lastError READ lastError NOTIFY lastErrorChanged)

public:
    explicit ProfileNegotiationCoordinator(QsfClient* qsfClient,
                                           MoonlightController* moonlight,
                                           QObject* parent = nullptr);

    bool busy() const;
    QString status() const;
    QString lastError() const;

    // The caller saves the desired Moonlight presentation first.  This method
    // then stops the current visible stream, waits for it to retire, commits
    // the guest-confirmed profile, and launches a new Moonlight process.
    Q_INVOKABLE bool negotiate(const QString& requestedResolution,
                               const QString& decoderPreference);
    Q_INVOKABLE void cancel();

signals:
    void busyChanged();
    void statusChanged();
    void lastErrorChanged();

private:
    enum class State {
        Idle,
        QuiescingMoonlight,
        WaitingForHostRetirement,
        WaitingForQsfReady,
        WaitingForProfile,
        WaitingForProfileSettlement,
        LaunchingMoonlight,
    };

    void beginDisplayNegotiationLease();
    void beginProfileSelection();
    void deactivateQsfForHandoff();
    void stopMoonlightForHandoff();
    void enterProfileSettlement(const QString& error);
    bool persistProfileSettlementMarker(qint64 deadlineUtcMs);
    void clearProfileSettlementMarker();
    void restoreProfileSettlementMarker();
    void receiveProfile(int width, int height, int fps, int bitrateKbps,
                        const QString& videoCodec, bool qemuApplied);
    void finishFailure(const QString& error);
    void setStatus(const QString& status);
    void setLastError(const QString& error);
    void setState(State state);

    QsfClient* m_QsfClient;
    MoonlightController* m_Moonlight;
    State m_State = State::Idle;
    QString m_RequestedResolution;
    QString m_DecoderPreference;
    QString m_Status;
    QString m_LastError;
    // These narrow synchronous guards distinguish the coordinator's own
    // teardown calls from a competing UI/API request while a scanout
    // transaction is in flight.
    bool m_ExpectedQsfDeactivation = false;
    bool m_ExpectedMoonlightTeardown = false;
    // `connection_optimize` is a remote QEMU/guest transaction, not a
    // cancellable local UI operation.  Once its encrypted request bytes have
    // left QsfClient, cancellation retains this lease until a terminal reply
    // arrives.  A transport failure instead enters a conservative settlement
    // window before any reconnect is permitted.
    QTimer* m_ProfileSettlementTimer = nullptr;
    bool m_ProfileRequestDispatched = false;
    bool m_CancelRequested = false;
    QString m_ProfileSettlementProfileId;
    // Invalidates an old host-retirement timer after cancellation/failure so
    // it cannot shorten a later, distinct profile handoff.
    quint64 m_HandoffGeneration = 0;
};
