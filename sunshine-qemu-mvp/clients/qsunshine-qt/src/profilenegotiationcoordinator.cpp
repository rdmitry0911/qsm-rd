// SPDX-License-Identifier: GPL-3.0-or-later

#include "profilenegotiationcoordinator.h"

#include "moonlightcontroller.h"
#include "qsfclient.h"

#include <QCryptographicHash>
#include <QDateTime>
#include <QSettings>
#include <QTimer>

#include <limits>

namespace {

// QProcess::finished means the local Moonlight process has exited, but the
// Sunshine capture/session cleanup happens asynchronously on the host.  This
// short, bounded interval prevents an old GameStream capture from replaying
// its previous SetUIInfo while QSF applies the next VirGL mode.
constexpr int kHostSessionRetirementGraceMs = 1000;

// A broker begins its bounded connection_optimize transaction only after the
// encrypted request has been sent.  If the return path fails after that
// point, closing the local TLS socket does not cancel the broker/gateway
// worker.  Keep a stopped Moonlight session and locked UI for longer than the
// 75-second broker transaction plus its transport margin before another
// stream can be launched.
constexpr int kProfileSettlementGraceMs = 90 * 1000;

// A live transport failure tells us when the remote request could last have
// been handed to the broker, so the normal 90-second settlement window is
// sufficient. A crash or normal app exit has no such observation: the bytes
// accepted by QSslSocket may reach the gateway near the client's 85-second
// request timeout and still run the broker's 75-second deadline. Persist this
// longer recovery guard before remote dispatch so a restarted client cannot
// reconnect into that interval.
constexpr int kCrashRecoveryProfileSettlementGraceMs = 180 * 1000;

constexpr auto kProfileSettlementDeadlineKey = "deadline-utc-ms";

qint64 utcNowMs()
{
    return QDateTime::currentDateTimeUtc().toMSecsSinceEpoch();
}

int boundedTimerInterval(qint64 milliseconds)
{
    return static_cast<int>(qMin(milliseconds,
                                 static_cast<qint64>(std::numeric_limits<int>::max())));
}

QString profileSettlementSettingsGroup(const QString& profileId)
{
    const QByteArray hash = QCryptographicHash::hash(
        profileId.normalized(QString::NormalizationForm_C).toUtf8(),
        QCryptographicHash::Sha256).toHex();
    return QStringLiteral("q-sunshine/client/profile-settlement/") +
           QString::fromLatin1(hash);
}

} // namespace

ProfileNegotiationCoordinator::ProfileNegotiationCoordinator(QsfClient* qsfClient,
                                                             MoonlightController* moonlight,
                                                             QObject* parent)
    : QObject(parent),
      m_QsfClient(qsfClient),
      m_Moonlight(moonlight),
      m_Status(QStringLiteral("No display-profile negotiation is in progress"))
{
    Q_ASSERT(m_QsfClient != nullptr);
    Q_ASSERT(m_Moonlight != nullptr);

    m_ProfileSettlementTimer = new QTimer(this);
    m_ProfileSettlementTimer->setSingleShot(true);
    m_ProfileSettlementTimer->setInterval(kProfileSettlementGraceMs);
    connect(m_ProfileSettlementTimer, &QTimer::timeout, this, [this]() {
        if (m_State != State::WaitingForProfileSettlement) {
            return;
        }
        // The client did not receive an authoritative terminal response, but
        // the remote transaction has had its full documented settlement
        // window.  QSF was already closed on entry to this state, so a manual
        // reconnect cannot overlap the old broker request anymore.
        m_ProfileRequestDispatched = false;
        m_CancelRequested = false;
        clearProfileSettlementMarker();
        ++m_HandoffGeneration;
        setState(State::Idle);
        setStatus(QStringLiteral("The uncertain display-profile transaction has settled; reconnect Moonlight manually if needed"));
    });

    connect(m_Moonlight, &MoonlightController::streamFinished, this, [this]() {
        if (m_State != State::QuiescingMoonlight) {
            return;
        }
        setState(State::WaitingForHostRetirement);
        setStatus(QStringLiteral("Moonlight stopped; waiting for Sunshine to retire the previous capture"));
        const quint64 handoffGeneration = m_HandoffGeneration;
        QTimer::singleShot(kHostSessionRetirementGraceMs, this,
                           [this, handoffGeneration]() {
                               if (m_HandoffGeneration == handoffGeneration) {
                                   beginDisplayNegotiationLease();
                               }
                           });
    });
    connect(m_Moonlight, &MoonlightController::streamTeardownStarted, this, [this]() {
        if (busy() && !m_ExpectedMoonlightTeardown) {
            finishFailure(QStringLiteral("Moonlight was stopped outside the display-profile handoff"));
        }
    });
    connect(m_Moonlight, &MoonlightController::streamStarted, this, [this]() {
        if (!busy()) {
            return;
        }
        // A live capture must never coexist with a still-pending QEMU/guest
        // profile commit.  The QML controls are locked during a handoff, but
        // this guard also protects callers using the controller API directly.
        finishFailure(QStringLiteral("Moonlight started before the display-profile handoff completed"));
        m_Moonlight->stopStream();
    });
    connect(m_QsfClient, &QsfClient::readyChanged, this, [this]() {
        if (m_State != State::WaitingForQsfReady || !m_QsfClient->ready()) {
            return;
        }
        beginProfileSelection();
    });
    connect(m_QsfClient, &QsfClient::connectionProfileRequestAboutToDispatch, this,
            [this]() {
                if (m_State == State::WaitingForProfile) {
                    if (!persistProfileSettlementMarker(
                            utcNowMs() + kCrashRecoveryProfileSettlementGraceMs)) {
                        // Do this synchronously before QsfClient writes a
                        // byte. Its encrypted-slot rechecks its request after
                        // this signal and will not dispatch a transaction
                        // whose crash-recovery marker could not be synced.
                        finishFailure(QStringLiteral("Cannot persist the display-profile recovery guard before dispatch"));
                    }
                }
            }, Qt::DirectConnection);
    connect(m_QsfClient, &QsfClient::connectionProfileRequestDispatched, this, [this]() {
        if (m_State != State::WaitingForProfile) {
            return;
        }
        m_ProfileRequestDispatched = true;
        setStatus(QStringLiteral("Waiting for the authoritative VirGL guest profile response"));
    });
    connect(m_QsfClient, &QsfClient::sessionActiveChanged, this, [this]() {
        if (busy() && !m_QsfClient->sessionActive() && !m_ExpectedQsfDeactivation) {
            finishFailure(QStringLiteral("QSF was deactivated outside the display-profile handoff"));
        }
    });
    connect(m_QsfClient, &QsfClient::connectionProfileReceived, this,
            [this](int width, int height, int fps, int bitrateKbps,
                   const QString& videoCodec, bool qemuApplied) {
                receiveProfile(width, height, fps, bitrateKbps, videoCodec, qemuApplied);
            });
    connect(m_QsfClient, &QsfClient::lastErrorChanged, this, [this]() {
        if (m_State != State::Idle && !m_QsfClient->lastError().isEmpty()) {
            if (m_State == State::WaitingForProfileSettlement) {
                return;
            }
            finishFailure(QStringLiteral("QSF profile negotiation failed: %1")
                              .arg(m_QsfClient->lastError()));
        }
    });
    connect(m_Moonlight, &MoonlightController::lastErrorChanged, this, [this]() {
        if (m_State != State::Idle && !m_Moonlight->lastError().isEmpty()) {
            finishFailure(QStringLiteral("Moonlight profile handoff failed: %1")
                              .arg(m_Moonlight->lastError()));
        }
    });
    connect(m_Moonlight, &MoonlightController::profileChanged, this, [this]() {
        // A durable marker is profile-scoped. Profile selection is disabled
        // while busy, so this can only activate a recovery guard before a
        // newly selected profile is allowed to create a graphics stream.
        if (!busy()) {
            restoreProfileSettlementMarker();
        }
    });

    // MoonlightController has already restored the selected desktop profile
    // before this coordinator is constructed, whereas QSF profile selection
    // occurs later from QML. Key recovery to Moonlight's current profile so
    // startup cannot accidentally inspect the QsfClient default profile.
    restoreProfileSettlementMarker();
}

bool ProfileNegotiationCoordinator::busy() const
{
    return m_State != State::Idle;
}

QString ProfileNegotiationCoordinator::status() const
{
    return m_Status;
}

QString ProfileNegotiationCoordinator::lastError() const
{
    return m_LastError;
}

bool ProfileNegotiationCoordinator::negotiate(const QString& requestedResolution,
                                               const QString& decoderPreference)
{
    if (busy()) {
        setLastError(QStringLiteral("A display-profile negotiation is already in progress"));
        return false;
    }
    if (!m_Moonlight->running() || m_Moonlight->streamStopping()) {
        setLastError(QStringLiteral("Keep the current Moonlight video visible before choosing a new stream profile"));
        return false;
    }
    if (!m_QsfClient->sessionActive() || !m_QsfClient->ready()) {
        setLastError(QStringLiteral("Activate and verify QSF for the visible Moonlight stream before choosing a profile"));
        return false;
    }

    m_RequestedResolution = requestedResolution.trimmed();
    m_DecoderPreference = decoderPreference.trimmed();
    if (m_RequestedResolution.isEmpty()) {
        setLastError(QStringLiteral("Select a client display size before choosing a stream profile"));
        return false;
    }
    ++m_HandoffGeneration;
    m_ProfileSettlementTimer->stop();
    m_ProfileRequestDispatched = false;
    m_CancelRequested = false;
    setLastError(QString());
    setState(State::QuiescingMoonlight);
    // Cancel all clipboard/file operations before the graphics process is
    // stopped.  The next temporary QSF lease is restricted to profile
    // selection, so no data operation can race SetUIInfo.
    deactivateQsfForHandoff();
    setStatus(QStringLiteral("Stopping the current Moonlight stream before changing the VirGL scanout"));
    stopMoonlightForHandoff();
    return true;
}

void ProfileNegotiationCoordinator::cancel()
{
    if (!busy()) {
        return;
    }
    if (m_State == State::WaitingForProfile && m_ProfileRequestDispatched) {
        // The broker may now be between SetUIInfo, guest COMMIT, and the
        // generation-bound ACK.  Do not abort its client socket: that only
        // loses our reply while allowing the remote change to continue.
        m_CancelRequested = true;
        setStatus(QStringLiteral("Cancellation requested; waiting for the in-flight guest profile transaction to finish"));
        return;
    }
    if (m_State == State::WaitingForProfileSettlement) {
        setStatus(QStringLiteral("Waiting for the uncertain guest profile transaction to settle before reconnecting"));
        return;
    }
    ++m_HandoffGeneration;
    m_ProfileSettlementTimer->stop();
    m_ProfileRequestDispatched = false;
    m_CancelRequested = false;
    clearProfileSettlementMarker();
    setState(State::Idle);
    m_QsfClient->setSessionActive(false);
    setStatus(QStringLiteral("Display-profile negotiation cancelled; reconnect Moonlight manually if needed"));
}

void ProfileNegotiationCoordinator::beginDisplayNegotiationLease()
{
    if (m_State != State::WaitingForHostRetirement) {
        return;
    }
    setState(State::WaitingForQsfReady);
    setStatus(QStringLiteral("Verifying a profile-only QSF lease before changing the VirGL scanout"));
    m_QsfClient->activateForDisplayNegotiation();
    if (!m_QsfClient->sessionActive() && !m_QsfClient->lastError().isEmpty()) {
        finishFailure(QStringLiteral("QSF profile negotiation could not start: %1")
                          .arg(m_QsfClient->lastError()));
    }
}

void ProfileNegotiationCoordinator::beginProfileSelection()
{
    if (m_State != State::WaitingForQsfReady) {
        return;
    }
    m_ProfileRequestDispatched = false;
    m_CancelRequested = false;
    setState(State::WaitingForProfile);
    setStatus(QStringLiteral("Selecting the common client decoder, host encoder, and VirGL guest profile"));
    m_QsfClient->optimizeConnectionForDisplay(m_RequestedResolution,
                                              m_DecoderPreference);
}

void ProfileNegotiationCoordinator::deactivateQsfForHandoff()
{
    const bool wasExpected = m_ExpectedQsfDeactivation;
    m_ExpectedQsfDeactivation = true;
    m_QsfClient->deactivateForProfileHandoff();
    m_ExpectedQsfDeactivation = wasExpected;
}

void ProfileNegotiationCoordinator::stopMoonlightForHandoff()
{
    const bool wasExpected = m_ExpectedMoonlightTeardown;
    m_ExpectedMoonlightTeardown = true;
    m_Moonlight->stopStream();
    m_ExpectedMoonlightTeardown = wasExpected;
}

void ProfileNegotiationCoordinator::enterProfileSettlement(const QString& error)
{
    if (m_State == State::WaitingForProfileSettlement) {
        return;
    }
    if (m_State != State::WaitingForProfile || !m_ProfileRequestDispatched) {
        finishFailure(error);
        return;
    }

    // The remote side has enough information to alter QEMU even though this
    // desktop can no longer prove its terminal result.  End the local
    // profile-only lease, but deliberately remain busy until the conservative
    // timer expires; UI/API attempts to reconnect stay blocked in this state.
    setState(State::WaitingForProfileSettlement);
    m_CancelRequested = false;
    deactivateQsfForHandoff();
    // A local failure now bounds the remaining remote work; update the
    // durable record to the same 90-second recovery window used in-process.
    persistProfileSettlementMarker(utcNowMs() + kProfileSettlementGraceMs);
    setLastError(error);
    setStatus(QStringLiteral("QSF lost the profile response; waiting for the remote VirGL transaction to settle"));
    m_ProfileSettlementTimer->start();
}

bool ProfileNegotiationCoordinator::persistProfileSettlementMarker(qint64 deadlineUtcMs)
{
    const QString profileId = m_Moonlight->currentProfileId();
    if (profileId.isEmpty()) {
        // MoonlightController always supplies a current profile, but never
        // silently skip crash protection if a future caller changes that
        // contract.
        return false;
    }

    m_ProfileSettlementProfileId = profileId;
    QSettings settings;
    settings.beginGroup(profileSettlementSettingsGroup(profileId));
    settings.setValue(QString::fromLatin1(kProfileSettlementDeadlineKey), deadlineUtcMs);
    settings.endGroup();
    // This record must survive an immediate window-manager quit/crash after
    // QSslSocket accepted the request bytes; do not leave it to destructor
    // flushing at process teardown.
    settings.sync();
    return settings.status() == QSettings::NoError;
}

void ProfileNegotiationCoordinator::clearProfileSettlementMarker()
{
    const QString profileId = m_ProfileSettlementProfileId.isEmpty()
        ? m_Moonlight->currentProfileId() : m_ProfileSettlementProfileId;
    if (!profileId.isEmpty()) {
        QSettings settings;
        settings.beginGroup(profileSettlementSettingsGroup(profileId));
        settings.remove(QString::fromLatin1(kProfileSettlementDeadlineKey));
        settings.endGroup();
        settings.sync();
    }
    m_ProfileSettlementProfileId.clear();
}

void ProfileNegotiationCoordinator::restoreProfileSettlementMarker()
{
    if (busy()) {
        return;
    }
    const QString profileId = m_Moonlight->currentProfileId();
    if (profileId.isEmpty()) {
        return;
    }

    QSettings settings;
    settings.beginGroup(profileSettlementSettingsGroup(profileId));
    const QVariant storedDeadline = settings.value(
        QString::fromLatin1(kProfileSettlementDeadlineKey));
    settings.endGroup();
    if (!storedDeadline.isValid()) {
        m_ProfileSettlementProfileId.clear();
        return;
    }

    bool deadlineOk = false;
    qint64 deadlineUtcMs = storedDeadline.toLongLong(&deadlineOk);
    const qint64 nowUtcMs = utcNowMs();
    if (!deadlineOk) {
        // A malformed local record must not turn an unproven remote QEMU
        // transaction into an immediate reconnect. Replace it with one full
        // crash-recovery interval for this exact desktop profile.
        deadlineUtcMs = nowUtcMs + kCrashRecoveryProfileSettlementGraceMs;
        persistProfileSettlementMarker(deadlineUtcMs);
    }
    if (deadlineUtcMs <= nowUtcMs) {
        m_ProfileSettlementProfileId = profileId;
        clearProfileSettlementMarker();
        return;
    }

    m_ProfileSettlementProfileId = profileId;
    setState(State::WaitingForProfileSettlement);
    setStatus(QStringLiteral("A previous display-profile transaction may still be settling; reconnect controls remain locked"));
    m_ProfileSettlementTimer->start(boundedTimerInterval(deadlineUtcMs - nowUtcMs));
}

void ProfileNegotiationCoordinator::receiveProfile(int width, int height, int fps,
                                                    int bitrateKbps,
                                                    const QString& videoCodec,
                                                    bool qemuApplied)
{
    if (m_State != State::WaitingForProfile) {
        return;
    }
    m_ProfileSettlementTimer->stop();
    m_ProfileRequestDispatched = false;
    clearProfileSettlementMarker();
    if (m_CancelRequested) {
        m_CancelRequested = false;
        ++m_HandoffGeneration;
        // The full profile reply is the authoritative terminal boundary.  Do
        // not launch a replacement graphics process after a user cancellation.
        setState(State::Idle);
        deactivateQsfForHandoff();
        setLastError(QString());
        setStatus(QStringLiteral("Display-profile handoff cancelled after the guest transaction completed"));
        return;
    }
    if (!qemuApplied) {
        finishFailure(QStringLiteral("QSF confirmed a profile without QEMU SetUIInfo; refusing to launch Moonlight"));
        return;
    }

    // The generation-bound ACK has already proved the exact guest scanout.
    // End the temporary lease before Moonlight starts so Sunshine is the only
    // scanout participant during the new graphics session.
    deactivateQsfForHandoff();
    setState(State::LaunchingMoonlight);
    setStatus(QStringLiteral("VirGL scanout confirmed; launching Moonlight with the negotiated profile"));
    QTimer::singleShot(0, this, [this, width, height, fps, bitrateKbps, videoCodec]() {
        if (m_State != State::LaunchingMoonlight) {
            return;
        }
        if (!m_Moonlight->applyNegotiatedProfileForHandoff(width, height, fps, bitrateKbps,
                                                            videoCodec)) {
            finishFailure(QStringLiteral("Moonlight rejected the guest-confirmed profile: %1")
                              .arg(m_Moonlight->lastError()));
            return;
        }
        setState(State::Idle);
        setStatus(QStringLiteral("Moonlight is starting with the guest-confirmed profile; activate QSF again only after video is visible"));
    });
}

void ProfileNegotiationCoordinator::finishFailure(const QString& error)
{
    if (m_State == State::Idle) {
        return;
    }
    if (m_State == State::WaitingForProfile && m_ProfileRequestDispatched) {
        enterProfileSettlement(error);
        return;
    }
    if (m_State == State::WaitingForProfileSettlement) {
        // Preserve the guard when a second local signal arrives while the
        // original broker transaction is still potentially mutating QEMU.
        setLastError(error);
        setStatus(QStringLiteral("Waiting for the uncertain guest profile transaction to settle"));
        return;
    }
    ++m_HandoffGeneration;
    m_ProfileSettlementTimer->stop();
    m_ProfileRequestDispatched = false;
    m_CancelRequested = false;
    clearProfileSettlementMarker();
    setState(State::Idle);
    // State becomes idle first so the synchronous sessionActiveChanged signal
    // from the deliberately cancelled lease cannot recurse into this method.
    m_QsfClient->setSessionActive(false);
    setLastError(error);
    setStatus(QStringLiteral("Display-profile negotiation failed"));
}

void ProfileNegotiationCoordinator::setStatus(const QString& status)
{
    if (m_Status == status) {
        return;
    }
    m_Status = status;
    emit statusChanged();
}

void ProfileNegotiationCoordinator::setLastError(const QString& error)
{
    if (m_LastError == error) {
        return;
    }
    m_LastError = error;
    emit lastErrorChanged();
}

void ProfileNegotiationCoordinator::setState(State state)
{
    const bool wasBusy = busy();
    m_State = state;
    // QML already disables its connection controls while busy.  Keep the
    // controller-level and QSF-property admission gates in lockstep so an
    // in-process caller cannot start a second capture or normal QSF session
    // during a remote scanout change/restart recovery window.
    m_Moonlight->setProfileHandoffStartBlocked(busy());
    m_QsfClient->setProfileHandoffOperationsBlocked(busy());
    if (wasBusy != busy()) {
        emit busyChanged();
    }
}
