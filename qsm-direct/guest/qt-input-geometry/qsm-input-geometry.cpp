// SPDX-License-Identifier: AGPL-3.0-or-later
// Public Qt input-method geometry only; no text or input-event interception.
#include <QGenericPlugin>
#include <QGuiApplication>
#include <QWindow>
#include <QInputMethod>
#include <QInputMethodEvent>
#include <QJsonObject>
#include <QJsonArray>
#include <QJsonDocument>
#include <QDBusConnection>
#include <QDBusAbstractAdaptor>
#include <QTimer>
#include <QPointer>
#include <QUuid>
#include <QtMath>
#include <QPlainTextEdit>
#include <QTextEdit>
class InputGeometry;
class GeometryAdaptor final : public QDBusAbstractAdaptor {
    Q_OBJECT
    Q_CLASSINFO("D-Bus Interface", "org.qsm.InputGeometry1")
public:
    explicit GeometryAdaptor(InputGeometry *parent);
public slots:
    QString Snapshot();
signals:
    void Changed(const QString &value);
};
class InputGeometry final : public QObject {
    Q_OBJECT
public:
    InputGeometry() : QObject(nullptr) {
        // QGuiApplication owns generic-plugin instances itself. Parenting
        // this object to the application would delete it a second time.
        QTimer::singleShot(0, this, [this] { start(); });
        connect(qGuiApp,&QCoreApplication::aboutToQuit,this,[this] {
            stopped_ = true; heartbeat_.stop();
            if (registered_) {
                auto bus = QDBusConnection::sessionBus();
                bus.unregisterObject("/org/qsm/InputGeometry");
                bus.unregisterService(QString("org.qsm.InputGeometry.p%1").arg(QCoreApplication::applicationPid()));
                registered_ = false;
            }
        });
    }
    QString snapshot() {
        auto *window = stopped_ ? nullptr : QGuiApplication::focusWindow();
        if (!window) { window_.clear(); token_.clear(); }
        else if (window != window_) { window_ = window; token_ = window ? QUuid::createUuid().toString(QUuid::WithoutBraces).remove('-') : QString(); }
        QJsonObject body{{"version",1},{"enabled",false},{"window",token_},
                         {"size",QJsonArray{window ? window->width() : 0,window ? window->height() : 0}},
                         {"rect",QJsonArray{0,0,0,0}},{"clip",QJsonArray{0,0,0,0}}};
        QInputMethodQueryEvent query(Qt::ImEnabled);
        if (!stopped_) {
            if (auto *focus = QGuiApplication::focusObject()) QCoreApplication::sendEvent(focus,&query);
        }
        if (window && window->isVisible() && query.value(Qt::ImEnabled).toBool()) {
            auto raw = QGuiApplication::inputMethod()->cursorRectangle();
            auto clipRaw = QGuiApplication::inputMethod()->inputItemClipRectangle();
            // Scroll-area input methods can omit the viewport's frame offset.
            // Use the editors' public viewport geometry, including arbitrary
            // styles and scroll positions, instead of adding a fixed border.
            QWidget *viewport = nullptr;
            QRect cursor;
            if (auto *plain = qobject_cast<QPlainTextEdit*>(QGuiApplication::focusObject())) {
                viewport = plain->viewport(); cursor = plain->cursorRect();
            } else if (auto *rich = qobject_cast<QTextEdit*>(QGuiApplication::focusObject())) {
                viewport = rich->viewport(); cursor = rich->cursorRect();
            }
            if (viewport && viewport->window()->windowHandle() == window) {
                const auto origin = viewport->mapTo(viewport->window(),QPoint(0,0));
                raw = QRectF(cursor.translated(origin));
                clipRaw = clipRaw.intersected(QRectF(viewport->rect().translated(origin)));
            }
            if (qIsFinite(raw.x()) && qIsFinite(raw.y()) && qIsFinite(raw.width()) && qIsFinite(raw.height()) &&
                    raw.x() >= -65536 && raw.y() >= -65536 && raw.x()+raw.width() <= 65536 && raw.y()+raw.height() <= 65536 &&
                    raw.height() > 0 && raw.height() <= 65536 && raw.width() >= 0) {
                if (!qIsFinite(clipRaw.x()) || !qIsFinite(clipRaw.y()) ||
                        !qIsFinite(clipRaw.width()) || !qIsFinite(clipRaw.height()) ||
                        clipRaw.x() < -65536 || clipRaw.y() < -65536 ||
                        clipRaw.x()+clipRaw.width() > 65536 || clipRaw.y()+clipRaw.height() > 65536 ||
                        clipRaw.width() <= 0 || clipRaw.height() <= 0) return finish(body);
                // Round the clip inward: a boundary pixel outside the input
                // must never acquire text-priority retention over moving video.
                const int left=qCeil(clipRaw.left()),top=qCeil(clipRaw.top());
                const auto clip=QRect(left,top,qFloor(clipRaw.x()+clipRaw.width())-left,
                    qFloor(clipRaw.y()+clipRaw.height())-top).intersected(QRect(QPoint(0,0),window->size()));
                auto rect = raw.toAlignedRect();
                if (!rect.width()) rect.setWidth(1);
                rect = rect.intersected(clip);
                if (!rect.isEmpty()) {
                    body["enabled"]=true;body["rect"]=QJsonArray{rect.x(),rect.y(),rect.width(),rect.height()};
                    body["clip"]=QJsonArray{clip.x(),clip.y(),clip.width(),clip.height()};
                }
            }
        }
        return finish(body);
    }
    QString finish(QJsonObject body) {
        if (body != body_) { body_ = body; ++sequence_; }
        body["sequence"] = double(sequence_);
        return QString::fromUtf8(QJsonDocument(body).toJson(QJsonDocument::Compact));
    }
signals:
    void Changed(const QString &value);
private:
    void schedule() {
        if (pending_ || stopped_) return;
        pending_ = true;
        QTimer::singleShot(20, this, [this] { pending_ = false; publish(false); });
    }
    void publish(bool heartbeat) {
        if (stopped_) return;
        const auto old = sequence_; const auto value = snapshot();
        if (sequence_ != old || (heartbeat && body_.value("enabled").toBool())) emit Changed(value);
    }
    void start() {
        if (stopped_) return;
        new GeometryAdaptor(this);
        auto bus = QDBusConnection::sessionBus();
        const auto service = QString("org.qsm.InputGeometry.p%1").arg(QCoreApplication::applicationPid());
        if (!bus.registerService(service) || !bus.registerObject("/org/qsm/InputGeometry",this,QDBusConnection::ExportAdaptors)) return;
        registered_ = true;
        connect(qGuiApp,&QGuiApplication::focusWindowChanged,this,[this] { schedule(); });
        connect(qGuiApp,&QGuiApplication::focusObjectChanged,this,[this] { schedule(); });
        auto *im = QGuiApplication::inputMethod();
        connect(im,&QInputMethod::cursorRectangleChanged,this,[this] { schedule(); });
        connect(im,&QInputMethod::inputItemClipRectangleChanged,this,[this] { schedule(); });
        heartbeat_.setInterval(750);
        connect(&heartbeat_,&QTimer::timeout,this,[this] { publish(true); });
        heartbeat_.start(); publish(false);
    }
    QPointer<QWindow> window_;
    QString token_;
    QJsonObject body_;
    quint64 sequence_ = 0;
    bool pending_ = false;
    bool stopped_ = false, registered_ = false;
    QTimer heartbeat_;
};
GeometryAdaptor::GeometryAdaptor(InputGeometry *parent):QDBusAbstractAdaptor(parent) { setAutoRelaySignals(true); }
QString GeometryAdaptor::Snapshot() { return static_cast<InputGeometry*>(parent())->snapshot(); }
class GeometryPlugin final : public QGenericPlugin {
    Q_OBJECT
    Q_PLUGIN_METADATA(IID QGenericPluginFactoryInterface_iid FILE "qsm-input-geometry.json")
public:
    QObject *create(const QString &key,const QString &) override {
        return key.compare("qsm-input-geometry",Qt::CaseInsensitive)==0 ? new InputGeometry : nullptr;
    }
};
#include "qsm-input-geometry.moc"
