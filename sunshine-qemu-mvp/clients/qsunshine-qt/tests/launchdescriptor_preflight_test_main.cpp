// SPDX-License-Identifier: GPL-3.0-or-later
// Strict local preflight coverage for the downloaded PVE launch envelope.

#include "systemauthclient.h"

#include <QCoreApplication>
#include <QDateTime>
#include <QFile>
#include <QFileDevice>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonParseError>
#include <QSettings>
#include <QStandardPaths>
#include <QTemporaryDir>
#include <QTemporaryFile>
#include <QTextStream>

class SystemAuthClientTestAccess
{
public:
    static bool descriptorMode(const SystemAuthClient& client)
    {
        return client.m_LaunchDescriptorMode;
    }

    static QString descriptorHost(const SystemAuthClient& client)
    {
        return client.m_LaunchHost;
    }

    static int descriptorPort(const SystemAuthClient& client)
    {
        return client.m_LaunchPort;
    }

    static QByteArray request(const SystemAuthClient& client)
    {
        return client.m_SecretRequest;
    }

    static bool descriptorSecretsCleared(const SystemAuthClient& client)
    {
        return !client.m_LaunchDescriptorMode && client.m_LaunchCaPem.isEmpty() &&
               client.m_SecretRequest.isEmpty();
    }
};

namespace {

// Public test-only certificate. Parsing the envelope must reject non-PEM
// values before networking; it does not rely on a host CA bundle being
// installed on the build runner.
const QByteArray kTestCertificate = QByteArrayLiteral(
    "-----BEGIN CERTIFICATE-----\n"
    "MIIDHTCCAgWgAwIBAgIUQkYy38xqtCoUiCRzRMLse8m7EHwwDQYJKoZIhvcNAQEL\n"
    "BQAwHjEcMBoGA1UEAwwTbGF1bmNoLXRlc3QuaW52YWxpZDAeFw0yNjA5MDIxMDIx\n"
    "MjJaFw0zNjA4MzAxMDIxMjJaMB4xHDAaBgNVBAMME2xhdW5jaC10ZXN0LmludmFs\n"
    "aWQwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQDQdxyZlYyKYG4aNIP9\n"
    "FbO7Q5TVrdV0yHcOIaN8VdaOs4GbtT4viSSg7oCq7U51+BTe8tRsGYlL4OxqBauI\n"
    "Ci/tUbT3Mff0TJsZ4vm9Fc7d2JWdVkfXPX659ujFbN3Me0OXVf18bO/uecVJZ8a+\n"
    "Mh8eGmqF1oc48TFCQoG5o9k+oo9j+DCHCtG5FIZ6Kp6ryUHQcA8Qp+NE8Bvz829x\n"
    "SikShDu+vuTSG9M46xcVhgXyNdANI0334MPg+6zgPC8TWTBwNRIyXzmFaXkEYEJM\n"
    "QF2i1REiSukuVpWJ4o2vdiUHsPzX+iDn1QE7X2FyJMQ3/aOlkVw0Ckv4hAEW08ro\n"
    "mFKzAgMBAAGjUzBRMB0GA1UdDgQWBBRw2qkBfFlSEK1cw/1Jao5621vl5zAfBgNV\n"
    "HSMEGDAWgBRw2qkBfFlSEK1cw/1Jao5621vl5zAPBgNVHRMBAf8EBTADAQH/MA0G\n"
    "CSqGSIb3DQEBCwUAA4IBAQCa9lkeO8jwa44G6nOUHRcQIQkKchlnSjb8In0C6hx+\n"
    "FyUt/WO0oUh+0ZQ9P30MH9wT5/YW2VDEK+4ZpEA3Hhdnp321hwnMSZB1BBhbserK\n"
    "7ng63he2jWwEUKKySY6C1Fg6AkhM224Ru/6XF671Rjv6nYJ0g9Cj3tsdAH/mkmv6\n"
    "gZ8JIWgJli9W5HKTMK92NGdZQKHcBiMxt9IOW+UMd0sYtyXDRFniy/R7TEbUUzHY\n"
    "5vh6iBDquu1AiOSsMajCGN7WyBav0EncRRVBHarVKzoxPiOILRt3yuVf1i4DmMqK\n"
    "y+O2U4S9onD1Mw7XYbVHAf46t+ICuc3j8+yuphPftTls\n"
    "-----END CERTIFICATE-----\n");

bool require(bool condition, const QString& message)
{
    if (!condition) {
        QTextStream(stderr) << "QSUNSHINE_LAUNCH_DESCRIPTOR_TEST_FAILED=" << message << '\n';
    }
    return condition;
}

QString testClaim()
{
    return QStringLiteral("qsd1.") + QString(43, QLatin1Char('A'));
}

QJsonObject validDescriptor()
{
    return QJsonObject {
        {QStringLiteral("version"), 1},
        {QStringLiteral("kind"), QStringLiteral("q-sunshine-pve-launch")},
        {QStringLiteral("endpoint"), QJsonObject {
             {QStringLiteral("host"), QStringLiteral("127.0.0.1")},
             {QStringLiteral("port"), 1},
             {QStringLiteral("server_name"), QStringLiteral("launch-test.invalid")},
             {QStringLiteral("ca_pem"), QString::fromLatin1(kTestCertificate)},
         }},
        {QStringLiteral("claim"), testClaim()},
        {QStringLiteral("expires_at_utc_ms"), QDateTime::currentMSecsSinceEpoch() + 60000},
    };
}

bool writeDescriptor(QTemporaryFile* file, const QJsonObject& object,
                     QFileDevice::Permissions permissions)
{
    if (file == nullptr || !file->open() ||
        !file->setPermissions(permissions)) {
        return false;
    }
    const QByteArray bytes = QJsonDocument(object).toJson(QJsonDocument::Compact);
    const bool written = file->write(bytes) == bytes.size() && file->flush();
    file->close();
    return written;
}

bool settingsContain(const QString& value)
{
    QSettings settings;
    for (const QString& key : settings.allKeys()) {
        if (settings.value(key).toString().contains(value)) {
            return true;
        }
    }
    return false;
}

bool rejectedDescriptor(const QJsonObject& object, QFileDevice::Permissions permissions)
{
    QTemporaryFile file;
    file.setAutoRemove(true);
    if (!writeDescriptor(&file, object, permissions)) {
        return false;
    }
    SystemAuthClient client;
    return !client.claimLaunchFile(file.fileName()) && !client.authenticating() &&
           !SystemAuthClientTestAccess::descriptorMode(client);
}

} // namespace

int main(int argc, char* argv[])
{
    QStandardPaths::setTestModeEnabled(true);
    QCoreApplication::setOrganizationName(QStringLiteral("q-sunshine-tests"));
    QCoreApplication::setOrganizationDomain(QStringLiteral("q-sunshine.local"));
    QCoreApplication::setApplicationName(QStringLiteral("qsunshine-launch-descriptor-test"));
    QCoreApplication application(argc, argv);

    const QFileDevice::Permissions browserDownloadPermissions =
        QFileDevice::ReadOwner | QFileDevice::WriteOwner |
        QFileDevice::ReadGroup | QFileDevice::ReadOther;
    QTemporaryFile descriptorFile;
    descriptorFile.setAutoRemove(true);
    if (!require(writeDescriptor(&descriptorFile, validDescriptor(), browserDownloadPermissions),
                 QStringLiteral("could not create browser-download-style descriptor"))) {
        return 2;
    }

    SystemAuthClient client;
    if (!require(client.claimLaunchFile(descriptorFile.fileName()),
                 QStringLiteral("valid descriptor was rejected before TLS redemption")) ||
        !require(client.authenticating() && SystemAuthClientTestAccess::descriptorMode(client) &&
                 SystemAuthClientTestAccess::descriptorHost(client) == QStringLiteral("127.0.0.1") &&
                 SystemAuthClientTestAccess::descriptorPort(client) == 1,
                 QStringLiteral("accepted descriptor did not remain only in transient launch state"))) {
        return 2;
    }
    QJsonParseError requestError;
    const QByteArray rawRequest = SystemAuthClientTestAccess::request(client);
    const QJsonDocument request = QJsonDocument::fromJson(rawRequest.trimmed(), &requestError);
    const QJsonObject expectedRequest {
        {QStringLiteral("version"), 1},
        {QStringLiteral("op"), QStringLiteral("redeem_launch")},
        {QStringLiteral("claim"), testClaim()},
    };
    if (!require(requestError.error == QJsonParseError::NoError && request.isObject() &&
                 request.object() == expectedRequest,
                 QStringLiteral("client did not serialize the exact redeem_launch request")) ||
        !require(!settingsContain(testClaim()),
                 QStringLiteral("launch claim was persisted to QSettings"))) {
        return 2;
    }
    client.logout();
    if (!require(SystemAuthClientTestAccess::descriptorSecretsCleared(client),
                 QStringLiteral("logout did not clear descriptor memory"))) {
        return 2;
    }

    QJsonObject unknownField = validDescriptor();
    unknownField.insert(QStringLiteral("unexpected"), true);
    if (!require(rejectedDescriptor(unknownField, browserDownloadPermissions),
                 QStringLiteral("descriptor with an unknown field was accepted"))) {
        return 2;
    }
    QJsonObject maliciousHost = validDescriptor();
    QJsonObject endpoint = maliciousHost.value(QStringLiteral("endpoint")).toObject();
    endpoint.insert(QStringLiteral("host"), QStringLiteral("broker.example:48123/path"));
    maliciousHost.insert(QStringLiteral("endpoint"), endpoint);
    if (!require(rejectedDescriptor(maliciousHost, browserDownloadPermissions),
                 QStringLiteral("descriptor with a route-injection host was accepted"))) {
        return 2;
    }
    QJsonObject expired = validDescriptor();
    expired.insert(QStringLiteral("expires_at_utc_ms"), QDateTime::currentMSecsSinceEpoch() - 1);
    if (!require(rejectedDescriptor(expired, browserDownloadPermissions),
                 QStringLiteral("expired descriptor was accepted"))) {
        return 2;
    }
    if (!require(rejectedDescriptor(validDescriptor(), browserDownloadPermissions |
                                                     QFileDevice::WriteOther),
                 QStringLiteral("world-writable descriptor was accepted"))) {
        return 2;
    }

    QTemporaryDir directory;
    const QString linkPath = directory.filePath(QStringLiteral("launch-link.qsm"));
    if (!require(directory.isValid() && QFile::link(descriptorFile.fileName(), linkPath),
                 QStringLiteral("could not create descriptor symlink fixture"))) {
        return 2;
    }
    SystemAuthClient symlinkClient;
    if (!require(!symlinkClient.claimLaunchFile(linkPath) && !symlinkClient.authenticating(),
                 QStringLiteral("descriptor symlink was accepted"))) {
        return 2;
    }

    QTextStream(stdout) << "QSUNSHINE_LAUNCH_DESCRIPTOR_PREFLIGHT_OK\n";
    return 0;
}
