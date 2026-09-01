// SPDX-License-Identifier: GPL-3.0-or-later
// Regression for cancellation after a remote display-profile transaction has
// already crossed the mTLS boundary.

#include "moonlightcontroller.h"
#include "profilenegotiationcoordinator.h"
#include "qsfclient.h"

#include <QCommandLineParser>
#include <QCoreApplication>
#include <QFile>
#include <QFileDevice>
#include <QFileInfo>
#include <QGuiApplication>
#include <QSaveFile>
#include <QSettings>
#include <QStandardPaths>
#include <QTextStream>
#include <QTemporaryDir>
#include <QTimer>

namespace {

struct Arguments {
    QString host;
    int port = 0;
    QString serverName;
    QString caFile;
    QString certificateFile;
    QString keyFile;
    QString cancelMarker;
};

bool require(bool condition, const QString& message)
{
    if (!condition) {
        QTextStream(stderr) << "PROFILE_CANCEL_REGRESSION_FAILED=" << message << '\n' << Qt::flush;
    }
    return condition;
}

bool writeFakeMoonlight(const QString& path)
{
    QSaveFile file(path);
    if (!file.open(QIODevice::WriteOnly)) {
        return false;
    }
    // The production controller only requires the stock CLI process
    // contract.  This intentionally tiny fixture lets the test distinguish
    // the original visible stream from a forbidden early replacement.
    const QByteArray script = R"SH(#!/bin/sh
trap 'exit 0' TERM INT
while :; do
  sleep 1
done
)SH";
    return file.write(script) == script.size() && file.commit() &&
           QFile::setPermissions(path, QFileDevice::ReadOwner | QFileDevice::WriteOwner |
                                       QFileDevice::ExeOwner);
}

class ProfileCancelRegression final : public QObject
{
public:
    ProfileCancelRegression(QGuiApplication& application, const Arguments& arguments)
        : QObject(&application),
          m_Application(application),
          m_Arguments(arguments),
          m_Qsf(this),
          m_Moonlight(this),
          m_Coordinator(&m_Qsf, &m_Moonlight, this)
    {
        m_Timeout.setSingleShot(true);
        m_Timeout.setInterval(15000);
        connect(&m_Timeout, &QTimer::timeout, this, [this]() {
            fail(QStringLiteral("timed out waiting for the delayed authoritative profile reply"));
        });

        m_CancelMarkerPoll.setInterval(20);
        connect(&m_CancelMarkerPoll, &QTimer::timeout, this, [this]() {
            maybeCancelAfterRemoteDispatch();
        });

        connect(&m_Moonlight, &MoonlightController::streamStarted, this, [this]() {
            ++m_StreamStarts;
            if (m_Phase == Phase::StartingInitial) {
                m_Phase = Phase::WaitingForInitialQsf;
                m_Qsf.setSessionActive(true);
                return;
            }
            fail(QStringLiteral("Moonlight started before the cancellation transaction reached a terminal reply"));
        });
        connect(&m_Qsf, &QsfClient::readyChanged, this, [this]() {
            if (!m_Qsf.ready() || m_Phase != Phase::WaitingForInitialQsf) {
                return;
            }
            m_Phase = Phase::WaitingForOptimizeDispatch;
            if (!m_Coordinator.negotiate(QStringLiteral("1280x720"), QStringLiteral("auto"))) {
                fail(QStringLiteral("could not start profile handoff: %1")
                         .arg(m_Coordinator.lastError()));
            }
        });
        connect(&m_Qsf, &QsfClient::statusChanged, this, [this]() {
            if (m_Phase == Phase::WaitingForOptimizeDispatch &&
                m_Qsf.status() == QStringLiteral("QSF connection_optimize in progress")) {
                m_Phase = Phase::WaitingForCancelMarker;
                m_CancelMarkerPoll.start();
            }
        });
        connect(&m_Qsf, &QsfClient::connectionProfileReceived, this,
                [this](int, int, int, int, const QString&, bool) {
                    receiveTerminalProfile();
                });
        connect(&m_Coordinator, &ProfileNegotiationCoordinator::lastErrorChanged,
                this, [this]() {
                    if (!m_Coordinator.lastError().isEmpty()) {
                        fail(QStringLiteral("coordinator reported an error: %1")
                                 .arg(m_Coordinator.lastError()));
                    }
                });
    }

    void start()
    {
        if (!require(QFileInfo::exists(m_Arguments.caFile) &&
                         QFileInfo::exists(m_Arguments.certificateFile) &&
                         QFileInfo::exists(m_Arguments.keyFile),
                     QStringLiteral("the mTLS fixture is incomplete")) ||
            !require(!QFileInfo::exists(m_Arguments.cancelMarker),
                     QStringLiteral("the cancellation marker must not exist before the test"))) {
            finish(2);
            return;
        }

        if (!m_Directory.isValid() ||
            !writeFakeMoonlight(m_Directory.filePath(QStringLiteral("fake-moonlight.sh")))) {
            fail(QStringLiteral("could not create the fake Moonlight executable"));
            return;
        }
        m_Moonlight.setBinaryPath(m_Directory.filePath(QStringLiteral("fake-moonlight.sh")));
        if (!m_Moonlight.saveProfile(QStringLiteral("profile-cancel-regression"),
                                     QStringLiteral("127.0.0.1"), QStringLiteral("Desktop"),
                                     QStringLiteral("1024x768"), QStringLiteral("windowed"))) {
            fail(QStringLiteral("could not save the initial Moonlight profile"));
            return;
        }
        m_Qsf.selectProfile(m_Moonlight.currentProfileId());
        if (!m_Qsf.applyConfiguration(m_Arguments.host, m_Arguments.port, m_Arguments.serverName,
                                      m_Arguments.caFile, m_Arguments.certificateFile,
                                      m_Arguments.keyFile)) {
            fail(QStringLiteral("could not configure QSF: %1").arg(m_Qsf.lastError()));
            return;
        }

        m_Timeout.start();
        m_Phase = Phase::StartingInitial;
        m_Moonlight.startStream(m_Moonlight.profileHost(), m_Moonlight.profileAppName(),
                                m_Moonlight.profileResolution(),
                                m_Moonlight.profileDisplayMode());
    }

private:
    enum class Phase {
        StartingInitial,
        WaitingForInitialQsf,
        WaitingForOptimizeDispatch,
        WaitingForCancelMarker,
        WaitingForTerminalProfile,
        Complete,
        Failed,
    };

    void maybeCancelAfterRemoteDispatch()
    {
        if (m_Phase != Phase::WaitingForCancelMarker ||
            !QFileInfo(m_Arguments.cancelMarker).isFile()) {
            return;
        }
        m_CancelMarkerPoll.stop();
        // The Python fixture creates this marker only after its fake guest
        // has received CONNECTION_OPTIMIZE.  This is strictly later than the
        // QSF client's local queueing/status transition.
        QTextStream(stdout) << "PROFILE_CANCEL_REMOTE_DISPATCH_CONFIRMED\n" << Qt::flush;
        // sessionActive is a writable QML property. It must not become a
        // second cancellation path after the broker has the request: that
        // would abort the mTLS socket and lose the terminal guest reply.
        m_Qsf.setSessionActive(false);
        if (!m_Qsf.sessionActive() || !m_Coordinator.busy() ||
            !m_Qsf.status().contains(QStringLiteral("handoff owns QSF deactivation"))) {
            fail(QStringLiteral("a direct QSF deactivation was not blocked during the in-flight profile transaction"));
            return;
        }
        m_ManualQsfDeactivationBlocked = true;
        QTextStream(stdout) << "PROFILE_CANCEL_QSF_DEACTIVATION_GATE_OK\n" << Qt::flush;
        m_Coordinator.cancel();
        if (!m_Coordinator.busy()) {
            fail(QStringLiteral("cancel unlocked the UI while connection_optimize was still in flight"));
            return;
        }
        // QML disables Connect while busy, but this calls the public
        // controller API directly too. The controller-level admission gate
        // must reject it before QProcess can create a second capture.
        m_Moonlight.startStream(m_Moonlight.profileHost(), m_Moonlight.profileAppName(),
                                m_Moonlight.profileResolution(),
                                m_Moonlight.profileDisplayMode());
        if (m_StreamStarts != 1 ||
            !m_Moonlight.status().contains(QStringLiteral("handoff owns"))) {
            fail(QStringLiteral("a direct Moonlight start was not blocked during the in-flight profile transaction"));
            return;
        }
        m_ManualStartBlocked = true;
        QTextStream(stdout) << "PROFILE_CANCEL_START_GATE_OK\n" << Qt::flush;
        m_Phase = Phase::WaitingForTerminalProfile;
        QTimer::singleShot(200, this, [this]() {
            if (m_Phase != Phase::WaitingForTerminalProfile) {
                return;
            }
            if (!m_ManualStartBlocked || !m_ManualQsfDeactivationBlocked ||
                !m_Coordinator.busy() || m_StreamStarts != 1 ||
                m_Moonlight.running()) {
                fail(QStringLiteral("cancellation released or relaunched Moonlight before the terminal profile reply"));
                return;
            }
            QTextStream(stdout) << "PROFILE_CANCEL_BUSY_GUARD_OK\n" << Qt::flush;
        });
    }

    void receiveTerminalProfile()
    {
        if (m_Phase != Phase::WaitingForTerminalProfile) {
            return;
        }
        // QsfClient emits this only after a complete broker reply. Defer the
        // assertion by one event turn so the coordinator's own receiver has
        // consumed that authoritative terminal state first.
        QTimer::singleShot(0, this, [this]() {
            if (m_Phase != Phase::WaitingForTerminalProfile) {
                return;
            }
            if (m_Coordinator.busy() || m_StreamStarts != 1 || m_Moonlight.running()) {
                fail(QStringLiteral("terminal reply did not release the cancelled handoff safely"));
                return;
            }
            QTextStream(stdout) << "PROFILE_CANCEL_TERMINAL_RELEASE_OK\n" << Qt::flush;
            m_Phase = Phase::Complete;
            m_Timeout.stop();
            finish(0);
        });
    }

    void fail(const QString& detail)
    {
        if (m_Phase == Phase::Failed || m_Phase == Phase::Complete) {
            return;
        }
        m_Phase = Phase::Failed;
        m_Timeout.stop();
        m_CancelMarkerPoll.stop();
        m_Qsf.setSessionActive(false);
        m_Moonlight.stopStream();
        QTextStream(stderr) << "PROFILE_CANCEL_REGRESSION_FAILED=" << detail << '\n' << Qt::flush;
        QTimer::singleShot(0, this, [this]() { finish(2); });
    }

    void finish(int result)
    {
        m_Qsf.setSessionActive(false);
        m_Moonlight.stopStream();
        QTimer::singleShot(0, &m_Application, [this, result]() { m_Application.exit(result); });
    }

    QGuiApplication& m_Application;
    Arguments m_Arguments;
    QsfClient m_Qsf;
    MoonlightController m_Moonlight;
    ProfileNegotiationCoordinator m_Coordinator;
    QTemporaryDir m_Directory;
    QTimer m_Timeout;
    QTimer m_CancelMarkerPoll;
    Phase m_Phase = Phase::StartingInitial;
    int m_StreamStarts = 0;
    bool m_ManualStartBlocked = false;
    bool m_ManualQsfDeactivationBlocked = false;
};

} // namespace

int main(int argc, char* argv[])
{
    QStandardPaths::setTestModeEnabled(true);
    QGuiApplication::setOrganizationName(QStringLiteral("q-sunshine-tests"));
    QGuiApplication::setOrganizationDomain(QStringLiteral("q-sunshine.local"));
    QGuiApplication::setApplicationName(QStringLiteral("qsunshine-profile-cancel-test"));
    QGuiApplication application(argc, argv);
    QSettings settings;
    settings.clear();
    settings.sync();

    QCommandLineParser parser;
    parser.addHelpOption();
    parser.addOption({QStringLiteral("host"), QStringLiteral("QSF gateway host"), QStringLiteral("host")});
    parser.addOption({QStringLiteral("port"), QStringLiteral("QSF gateway port"), QStringLiteral("port")});
    parser.addOption({QStringLiteral("server-name"), QStringLiteral("QSF TLS server name"), QStringLiteral("name")});
    parser.addOption({QStringLiteral("ca-file"), QStringLiteral("QSF CA PEM"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("cert-file"), QStringLiteral("QSF client certificate PEM"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("key-file"), QStringLiteral("QSF client key PEM"), QStringLiteral("path")});
    parser.addOption({QStringLiteral("cancel-marker"),
                      QStringLiteral("created by the fixture after remote optimize dispatch"),
                      QStringLiteral("path")});
    parser.process(application);

    bool portOk = false;
    const int port = parser.value(QStringLiteral("port")).toInt(&portOk);
    Arguments arguments;
    arguments.host = parser.value(QStringLiteral("host"));
    arguments.port = port;
    arguments.serverName = parser.value(QStringLiteral("server-name"));
    arguments.caFile = parser.value(QStringLiteral("ca-file"));
    arguments.certificateFile = parser.value(QStringLiteral("cert-file"));
    arguments.keyFile = parser.value(QStringLiteral("key-file"));
    arguments.cancelMarker = parser.value(QStringLiteral("cancel-marker"));
    if (!portOk || port < 1 || port > 65535 || arguments.host.trimmed().isEmpty() ||
        arguments.caFile.isEmpty() || arguments.certificateFile.isEmpty() ||
        arguments.keyFile.isEmpty() || arguments.cancelMarker.isEmpty()) {
        QTextStream(stderr) << "invalid profile cancellation regression arguments\n";
        return 2;
    }

    ProfileCancelRegression regression(application, arguments);
    QTimer::singleShot(0, &regression, [&regression]() { regression.start(); });
    return application.exec();
}
