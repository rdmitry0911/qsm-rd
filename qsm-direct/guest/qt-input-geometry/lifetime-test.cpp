// SPDX-License-Identifier: AGPL-3.0-or-later
#include <QApplication>
#include <QGenericPluginFactory>
#include <QTimer>
#include <cstdio>
#include <memory>
#include <cstring>

int main(int argc,char **argv) {
    int result = 0;
    {
        std::unique_ptr<QGuiApplication> application;
        if (argc>1 && std::strcmp(argv[1],"widgets")==0)
            application = std::make_unique<QApplication>(argc,argv);
        else application = std::make_unique<QGuiApplication>(argc,argv);
        if (!QGenericPluginFactory::keys().contains("qsm-input-geometry")) return 2;
        QTimer::singleShot(50,application.get(),&QCoreApplication::quit);
        result = application->exec();
    }
    // The ownership regression crashes while destroying the application,
    // after exec() returned successfully. Print only after full destruction.
    std::puts("QSM_QT_GEOMETRY_LIFETIME_OK");
    return result;
}
