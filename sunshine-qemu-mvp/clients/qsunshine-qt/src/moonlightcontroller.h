// SPDX-License-Identifier: GPL-3.0-or-later
// Local process controller: Moonlight remains the graphics/media endpoint.

#pragma once

#include <QByteArray>
#include <QObject>
#include <QProcess>
#include <QString>
#include <QStringList>
#include <QtGlobal>

class QTimer;

class MoonlightController final : public QObject
{
    Q_OBJECT

    Q_PROPERTY(QString binaryPath READ binaryPath WRITE setBinaryPath NOTIFY binaryPathChanged)
    Q_PROPERTY(bool running READ running NOTIFY runningChanged)
    Q_PROPERTY(bool streamBusy READ streamBusy NOTIFY streamBusyChanged)
    Q_PROPERTY(bool streamStopping READ streamStopping NOTIFY streamStoppingChanged)
    Q_PROPERTY(bool pairing READ pairing NOTIFY pairingChanged)
    Q_PROPERTY(QString status READ status NOTIFY statusChanged)
    Q_PROPERTY(QString lastError READ lastError NOTIFY lastErrorChanged)
    Q_PROPERTY(QString recentOutput READ recentOutput NOTIFY recentOutputChanged)
    Q_PROPERTY(QStringList profileIds READ profileIds NOTIFY profilesChanged)
    Q_PROPERTY(QString currentProfileId READ currentProfileId NOTIFY profileChanged)
    Q_PROPERTY(QString profileHost READ profileHost NOTIFY profileChanged)
    Q_PROPERTY(QString profileAppName READ profileAppName NOTIFY profileChanged)
    Q_PROPERTY(QString profileResolution READ profileResolution NOTIFY profileChanged)
    Q_PROPERTY(QString profileDisplayMode READ profileDisplayMode NOTIFY profileChanged)
    Q_PROPERTY(int profileFps READ profileFps NOTIFY profileChanged)
    Q_PROPERTY(int profileBitrateKbps READ profileBitrateKbps NOTIFY profileChanged)
    Q_PROPERTY(QString profileVideoCodec READ profileVideoCodec NOTIFY profileChanged)
    Q_PROPERTY(QString videoDecoder READ videoDecoder WRITE setVideoDecoder NOTIFY videoDecoderChanged)

public:
    explicit MoonlightController(QObject* parent = nullptr);

    QString binaryPath() const;
    bool running() const;
    bool streamBusy() const;
    bool streamStopping() const;
    bool pairing() const;
    QString status() const;
    QString lastError() const;
    QString recentOutput() const;
    QStringList profileIds() const;
    QString currentProfileId() const;
    QString profileHost() const;
    QString profileAppName() const;
    QString profileResolution() const;
    QString profileDisplayMode() const;
    int profileFps() const;
    int profileBitrateKbps() const;
    QString profileVideoCodec() const;
    QString videoDecoder() const;

    void setBinaryPath(const QString& binaryPath);
    Q_INVOKABLE void setVideoDecoder(const QString& videoDecoder);

    Q_INVOKABLE bool selectProfile(const QString& profileId);
    Q_INVOKABLE bool saveProfile(const QString& profileId, const QString& host,
                                 const QString& appName, const QString& resolution,
                                 const QString& displayMode);
    Q_INVOKABLE void pair(const QString& host, const QString& pin);
    Q_INVOKABLE void cancelPairing();
    Q_INVOKABLE void startStream(const QString& host, const QString& appName,
                                 const QString& resolution, const QString& displayMode);
    Q_INVOKABLE void stopStream();
    bool applyNegotiatedProfile(int width, int height, int fps, int bitrateKbps,
                                const QString& videoCodec);

signals:
    void binaryPathChanged();
    void runningChanged();
    void streamBusyChanged();
    void streamStoppingChanged();
    void pairingChanged();
    void statusChanged();
    void lastErrorChanged();
    void recentOutputChanged();
    void profilesChanged();
    void profileChanged();
    void videoDecoderChanged();
    // Emitted only after the Moonlight pairing child has actually started.
    // `pairingChanged(true)` is deliberately earlier so UI can disable its
    // controls while QProcess is still being created.
    void pairProcessStarted();
    void streamStarted();
    void streamTeardownStarted();
    void streamFinished();

private:
    friend class ProfileNegotiationCoordinator;

    struct StreamRequest {
        QString host;
        QString appName;
        QString resolution;
        QString displayMode;
        int fps = 0;
        int bitrateKbps = 0;
        QString videoCodec;
    };

    bool validateStreamRequest(const StreamRequest& request, QString* error) const;
    bool validateExecutable(QString* error) const;
    bool applyNegotiatedProfileForHandoff(int width, int height, int fps, int bitrateKbps,
                                          const QString& videoCodec);
    // Only the coordinator can enter or release this guard. Keeping its
    // mutator private prevents an arbitrary in-process caller from reopening
    // normal Moonlight admission during a remote guest transaction.
    void setProfileHandoffStartBlocked(bool blocked);
    static bool validProfileId(const QString& profileId, QString* error);
    QString profileSettingsGroup(const QString& profileId) const;
    void loadProfile(const QString& profileId);
    void writeProfileIndex() const;
    void writeCurrentProfile() const;
    void startValidatedStreamRequest(const StreamRequest& request);
    void launchStream(const StreamRequest& request);
    void appendOutput(const QByteArray& output);
    void appendRedactedOutput(const QByteArray& output);
    void flushOutputFragment();
    // A profile handoff has already selected an encoder/guest profile against
    // the current desktop configuration.  Reject public configuration changes
    // until its remote transaction is terminal, rather than letting a caller
    // mutate the later Moonlight launch target underneath the coordinator.
    bool profileHandoffBlocksConfigurationChange(const QString& operation);
    void setStatus(const QString& status);
    void setLastError(const QString& error);
    void setRunning(bool running);
    void setPairing(bool pairing);
    void finishStream(int exitCode, QProcess::ExitStatus exitStatus);

    QString m_BinaryPath;
    bool m_Running;
    bool m_Pairing;
    QString m_Status;
    QString m_LastError;
    QString m_RecentOutput;
    QByteArray m_OutputFragment;
    bool m_DiscardingOutputLine;
    QStringList m_ProfileIds;
    QString m_CurrentProfileId;
    QString m_ProfileHost;
    QString m_ProfileAppName;
    QString m_ProfileResolution;
    QString m_ProfileDisplayMode;
    int m_ProfileFps;
    int m_ProfileBitrateKbps;
    QString m_ProfileVideoCodec;
    QString m_VideoDecoder;
    QProcess* m_StreamProcess;
    QProcess* m_PairProcess;
    QTimer* m_StopTimer;
    StreamRequest m_PendingRestart;
    bool m_HasPendingRestart;
    bool m_StopRequested;
    bool m_ProfileHandoffStartBlocked;
    bool m_PairCancelRequested;
    quint64 m_PairGeneration;
    quint64 m_RestartGeneration;
    quint64 m_PendingRestartGeneration;
};
