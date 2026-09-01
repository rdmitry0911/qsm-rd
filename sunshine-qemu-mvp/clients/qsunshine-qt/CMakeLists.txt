find_package(Qt6 6.4 REQUIRED COMPONENTS Core Gui Network Quick QuickControls2)

qt_standard_project_setup()
include(GNUInstallDirs)

qt_add_executable(qsunshine-client
    src/main.cpp
    src/moonlightcontroller.cpp
    src/moonlightcontroller.h
    src/profilenegotiationcoordinator.cpp
    src/profilenegotiationcoordinator.h
    src/qsfclient.cpp
    src/qsfclient.h
)

target_link_libraries(qsunshine-client PRIVATE
    Qt6::Core
    Qt6::Gui
    Qt6::Network
    Qt6::Quick
    Qt6::QuickControls2
)

qt_add_resources(qsunshine-client qsunshine_client_qml
    PREFIX "/"
    BASE "${CMAKE_CURRENT_SOURCE_DIR}/qml"
    FILES
        qml/Main.qml
)

set_target_properties(qsunshine-client PROPERTIES
    OUTPUT_NAME qsunshine-client
)

if(APPLE)
    # Produce a conventional .app. The Tahoe packaging script then deploys Qt
    # frameworks and embeds an untouched Moonlight.app in Resources.
    set_target_properties(qsunshine-client PROPERTIES
        MACOSX_BUNDLE TRUE
        MACOSX_BUNDLE_BUNDLE_NAME "q-sunshine"
        MACOSX_BUNDLE_GUI_IDENTIFIER "io.qsunshine.client"
        MACOSX_BUNDLE_BUNDLE_VERSION "${PROJECT_VERSION}"
        MACOSX_BUNDLE_SHORT_VERSION_STRING "${PROJECT_VERSION}"
    )
endif()

# The real-runtime driver links the exact process and QSF classes used by the
# GUI.  It is intentionally not a CTest: a surrounding harness supplies a
# disposable Sunshine/QEMU/VirGL system and performs visual attestation before
# it creates the activation markers accepted by the driver.
qt_add_executable(qsunshine-real-e2e-driver
    tests/real_e2e_main.cpp
    src/moonlightcontroller.cpp
    src/moonlightcontroller.h
    src/profilenegotiationcoordinator.cpp
    src/profilenegotiationcoordinator.h
    src/qsfclient.cpp
    src/qsfclient.h
)
target_include_directories(qsunshine-real-e2e-driver PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
)
target_link_libraries(qsunshine-real-e2e-driver PRIVATE
    Qt6::Core
    Qt6::Gui
    Qt6::Network
)

# QML is embedded, but a matching Qt/QML runtime is still needed at install
# time.  Platform packaging/deployment is handled by the Qt packaging tool on
# the target platform rather than making the Linux build assume a bundle form.
install(TARGETS qsunshine-client
    RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
    BUNDLE DESTINATION .
)

if(BUILD_TESTING)
    qt_add_executable(qsunshine-qml-smoke
        tests/qml_smoke_main.cpp
        src/moonlightcontroller.cpp
        src/moonlightcontroller.h
        src/profilenegotiationcoordinator.cpp
        src/profilenegotiationcoordinator.h
        src/qsfclient.cpp
        src/qsfclient.h
    )
    target_include_directories(qsunshine-qml-smoke PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/src
    )
    target_link_libraries(qsunshine-qml-smoke PRIVATE
        Qt6::Core
        Qt6::Gui
        Qt6::Network
        Qt6::Quick
        Qt6::QuickControls2
    )
    qt_add_resources(qsunshine-qml-smoke qsunshine_qml_smoke_qml
        PREFIX "/"
        BASE "${CMAKE_CURRENT_SOURCE_DIR}/qml"
        FILES
            qml/Main.qml
    )
    if(UNIX)
        add_test(NAME qsunshine_qml_smoke COMMAND qsunshine-qml-smoke)
        set_tests_properties(qsunshine_qml_smoke PROPERTIES
            TIMEOUT 15
            ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_QUICK_BACKEND=software"
        )
    endif()

    # The controller test generates a POSIX shell fake Moonlight binary.
    # Production code remains portable; only this process-contract fixture is
    # Unix-specific until it is replaced with a cross-platform fake child.
    if(UNIX)
        qt_add_executable(qsunshine-moonlight-controller-test
            tests/moonlightcontroller_test_main.cpp
            src/moonlightcontroller.cpp
            src/moonlightcontroller.h
        )
        target_include_directories(qsunshine-moonlight-controller-test PRIVATE
            ${CMAKE_CURRENT_SOURCE_DIR}/src
        )
        target_link_libraries(qsunshine-moonlight-controller-test PRIVATE
            Qt6::Core
        )
        add_test(NAME qsunshine_moonlight_controller
                 COMMAND qsunshine-moonlight-controller-test)
        set_tests_properties(qsunshine_moonlight_controller PROPERTIES TIMEOUT 25)
    endif()

    qt_add_executable(qsunshine-qsf-e2e
        tests/qsf_e2e_main.cpp
        src/qsfclient.cpp
        src/qsfclient.h
    )
    target_include_directories(qsunshine-qsf-e2e PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/src
    )
    target_link_libraries(qsunshine-qsf-e2e PRIVATE
        Qt6::Core
        Qt6::Gui
        Qt6::Network
    )

    # This driver intentionally uses the same real mTLS QSF client and
    # process controller as the desktop shell.  Its Python fixture delays the
    # authoritative guest ACK after the broker has accepted
    # connection_optimize, so it can prove that cancelling at that point does
    # not unlock a still-changing scanout or launch a replacement Moonlight
    # process early.
    qt_add_executable(qsunshine-profile-negotiation-cancel-test
        tests/profile_negotiation_cancel_test_main.cpp
        src/moonlightcontroller.cpp
        src/moonlightcontroller.h
        src/profilenegotiationcoordinator.cpp
        src/profilenegotiationcoordinator.h
        src/qsfclient.cpp
        src/qsfclient.h
    )
    target_include_directories(qsunshine-profile-negotiation-cancel-test PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/src
    )
    target_link_libraries(qsunshine-profile-negotiation-cancel-test PRIVATE
        Qt6::Core
        Qt6::Gui
        Qt6::Network
    )

    # A restart can happen after QSslSocket accepted connection_optimize but
    # before its QEMU/guest result reaches the desktop.  This no-network
    # regression seeds the per-Moonlight-profile durable marker before the
    # coordinator is constructed, verifies both normal admission gates, then
    # verifies synchronous expiry cleanup using a past deadline.
    if(UNIX)
        qt_add_executable(qsunshine-profile-settlement-recovery-test
            tests/profile_settlement_recovery_test_main.cpp
            src/moonlightcontroller.cpp
            src/moonlightcontroller.h
            src/profilenegotiationcoordinator.cpp
            src/profilenegotiationcoordinator.h
            src/qsfclient.cpp
            src/qsfclient.h
        )
        target_include_directories(qsunshine-profile-settlement-recovery-test PRIVATE
            ${CMAKE_CURRENT_SOURCE_DIR}/src
        )
        target_link_libraries(qsunshine-profile-settlement-recovery-test PRIVATE
            Qt6::Core
            Qt6::Gui
            Qt6::Network
        )
        add_test(NAME qsunshine_qt_profile_settlement_recovery
                 COMMAND qsunshine-profile-settlement-recovery-test)
        set_tests_properties(qsunshine_qt_profile_settlement_recovery PROPERTIES
            TIMEOUT 20
            ENVIRONMENT "QT_QPA_PLATFORM=offscreen"
        )
    endif()

    find_package(Python3 COMPONENTS Interpreter)
    find_program(QSUNSHINE_OPENSSL_EXECUTABLE openssl)
    # The current gateway fixture uses an AF_UNIX broker and POSIX select()
    # behavior, so it is a Unix test even though QsfClient itself is portable.
    if(UNIX AND Python3_Interpreter_FOUND AND QSUNSHINE_OPENSSL_EXECUTABLE)
        add_test(
            NAME qsunshine_qt_qsf_mtls_e2e
            COMMAND ${Python3_EXECUTABLE}
                ${CMAKE_SOURCE_DIR}/tests/qsf_control/test_qsf_qt_client.py
                --qt-client $<TARGET_FILE:qsunshine-qsf-e2e>
                -v
        )
        set_tests_properties(qsunshine_qt_qsf_mtls_e2e PROPERTIES
            TIMEOUT 110
            ENVIRONMENT "QT_QPA_PLATFORM=offscreen"
        )
        add_test(
            NAME qsunshine_qt_profile_cancel_regression
            COMMAND ${Python3_EXECUTABLE}
                ${CMAKE_SOURCE_DIR}/tests/qsf_control/test_qsf_qt_profile_cancel.py
                --qt-client $<TARGET_FILE:qsunshine-profile-negotiation-cancel-test>
                -v
        )
        set_tests_properties(qsunshine_qt_profile_cancel_regression PROPERTIES
            TIMEOUT 35
            ENVIRONMENT "QT_QPA_PLATFORM=offscreen"
        )
    endif()
endif()
