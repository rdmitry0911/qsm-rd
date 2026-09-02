cmake_minimum_required(VERSION 3.20)
project(sunshine_qemu_mvp VERSION 0.4.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(QMDP_BUILD_DBUS "Build the QEMU D-Bus implementation" ON)
option(QMDP_BUILD_TOOLS "Build command-line probes and test services" ON)
option(QMDP_ENABLE_SANITIZERS "Enable AddressSanitizer and UndefinedBehaviorSanitizer" OFF)
option(QMDP_ENABLE_REAL_QEMU_E2E
       "Register the CPU/TCG real-QEMU E2E test when host tools are available"
       ON)
option(QMDP_ENABLE_DMABUF_READBACK
       "Enable optional GBM DMA-BUF to CPU readback for QEMU Display1"
       ON)
option(QMDP_BUILD_QT_CLIENT
       "Build the standalone Qt q-sunshine desktop shell (requires Qt 6)"
       OFF)

include(CTest)
find_package(Threads REQUIRED)

set(QMDP_WARNINGS
    $<$<CXX_COMPILER_ID:GNU,Clang>:-Wall;-Wextra;-Wpedantic;-Wconversion;-Wshadow>
)
set(QMDP_SANITIZERS)
if(QMDP_ENABLE_SANITIZERS AND CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    list(APPEND QMDP_SANITIZERS
        -fsanitize=address,undefined
        -fno-omit-frame-pointer
    )
endif()

if(QMDP_BUILD_QT_CLIENT)
    add_subdirectory(clients/qsunshine-qt)
endif()

function(qmdp_target_defaults target)
    target_compile_options(${target} PRIVATE ${QMDP_WARNINGS} ${QMDP_SANITIZERS})
    if(QMDP_SANITIZERS)
        target_link_options(${target} PRIVATE ${QMDP_SANITIZERS})
    endif()
endfunction()

add_library(qmdp_core
    src/capture/cpu_framebuffer.cpp
    src/capture/mapped_region.cpp
    src/core/audio_fifo.cpp
    src/core/pixel_format.cpp
    src/core/resize_coalescer.cpp
    src/core/session.cpp
)
target_include_directories(qmdp_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(qmdp_core PUBLIC Threads::Threads)
qmdp_target_defaults(qmdp_core)

add_library(qmdp_pipeline
    src/pipeline/desktop_session.cpp
)
target_include_directories(qmdp_pipeline PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(qmdp_pipeline PUBLIC qmdp_core Threads::Threads)
qmdp_target_defaults(qmdp_pipeline)

add_library(qmdp_software
    src/software/cpu_frame_sink.cpp
    src/software/ffmpeg_software_adapter.cpp
)
target_include_directories(qmdp_software PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(qmdp_software PUBLIC qmdp_core Threads::Threads)
qmdp_target_defaults(qmdp_software)

add_library(qmdp_mock
    src/mock/mock_qemu_display.cpp
    src/mock/mock_sunshine_adapter.cpp
)
target_include_directories(qmdp_mock PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(qmdp_mock PUBLIC qmdp_core Threads::Threads)
qmdp_target_defaults(qmdp_mock)

add_executable(qmdp_demo src/demo/main.cpp)
target_link_libraries(qmdp_demo PRIVATE qmdp_core qmdp_mock Threads::Threads)
qmdp_target_defaults(qmdp_demo)

if(QMDP_BUILD_DBUS)
    # Minimal CI images commonly provide libsystemd.so.0 without
    # libsystemd-dev. src/compat/sd_bus_compat.h declares the small stable ABI
    # subset used by this MVP when the official header is unavailable.
    find_file(QMDP_SYSTEMD_LIBRARY
        NAMES libsystemd.so libsystemd.so.0
        PATHS
            /lib
            /lib64
            /lib/x86_64-linux-gnu
            /lib/aarch64-linux-gnu
            /usr/lib
            /usr/lib64
            /usr/lib/x86_64-linux-gnu
            /usr/lib/aarch64-linux-gnu
    )
    if(NOT QMDP_SYSTEMD_LIBRARY)
        message(FATAL_ERROR "QMDP_BUILD_DBUS=ON but libsystemd could not be found")
    endif()

    add_library(qmdp_dbus
        src/dbus/sd_bus.cpp
        src/qemu/qemu_dbus_display.cpp
    )
    target_include_directories(qmdp_dbus PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
    target_link_libraries(qmdp_dbus PUBLIC
        qmdp_core Threads::Threads ${QMDP_SYSTEMD_LIBRARY}
    )
    qmdp_target_defaults(qmdp_dbus)

    if(QMDP_ENABLE_DMABUF_READBACK)
        find_package(PkgConfig QUIET)
        if(PkgConfig_FOUND)
            pkg_check_modules(QMDP_GBM QUIET IMPORTED_TARGET gbm)
            pkg_check_modules(QMDP_DRM QUIET IMPORTED_TARGET libdrm)
            pkg_check_modules(QMDP_EPOXY QUIET IMPORTED_TARGET epoxy)
        endif()
        if(TARGET PkgConfig::QMDP_GBM AND TARGET PkgConfig::QMDP_DRM AND
           TARGET PkgConfig::QMDP_EPOXY)
            target_sources(qmdp_dbus PRIVATE
                src/capture/dmabuf_readback.cpp
            )
            target_link_libraries(qmdp_dbus PRIVATE
                PkgConfig::QMDP_GBM
                PkgConfig::QMDP_DRM
                PkgConfig::QMDP_EPOXY
            )
            target_compile_definitions(qmdp_dbus PUBLIC QMDP_HAS_GBM=1 QMDP_HAS_EGL_READBACK=1)
        else()
            message(STATUS "QMDP DMA-BUF readback disabled: GBM/libdrm/libepoxy development files were not found")
        endif()
    endif()
endif()

if(QMDP_BUILD_TOOLS AND QMDP_BUILD_DBUS)
    add_executable(qemu-display-probe tools/qemu_display_probe.cpp)
    target_link_libraries(qemu-display-probe PRIVATE
        qmdp_dbus qmdp_pipeline qmdp_software Threads::Threads
    )
    qmdp_target_defaults(qemu-display-probe)

    # Standalone fake QEMU service for a real cross-process message-bus test.
    add_library(qmdp_testing
        src/testing/fake_qemu_service.cpp
    )
    target_include_directories(qmdp_testing PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
    target_link_libraries(qmdp_testing PUBLIC
        qmdp_dbus qmdp_core Threads::Threads ${QMDP_SYSTEMD_LIBRARY}
    )
    qmdp_target_defaults(qmdp_testing)

    add_executable(qmdp-fake-qemu tools/fake_qemu_dbus.cpp)
    target_link_libraries(qmdp-fake-qemu PRIVATE qmdp_testing)
    qmdp_target_defaults(qmdp-fake-qemu)
endif()

if(BUILD_TESTING)
    find_package(Python3 COMPONENTS Interpreter)

    if(Python3_Interpreter_FOUND)
        add_test(
            NAME qsf_control_protocol
            COMMAND ${Python3_EXECUTABLE}
                ${CMAKE_CURRENT_SOURCE_DIR}/tests/qsf_control/test_qsf_control.py
                -v
        )
        set_tests_properties(qsf_control_protocol PROPERTIES TIMEOUT 20)

        add_test(
            NAME qsf_guest_agent_pty
            COMMAND ${Python3_EXECUTABLE}
                ${CMAKE_CURRENT_SOURCE_DIR}/tests/qsf_control/test_qsf_guest_agent.py
                -v
        )
        set_tests_properties(qsf_guest_agent_pty PROPERTIES TIMEOUT 20)

        add_test(
            NAME qsf_virgl_profile_fixture_contract
            COMMAND ${Python3_EXECUTABLE}
                ${CMAKE_CURRENT_SOURCE_DIR}/tests/qsf_control/test_virgl_qsf_profile_fixture.py
                -v
        )
        set_tests_properties(qsf_virgl_profile_fixture_contract PROPERTIES TIMEOUT 10)

        find_program(QMDP_OPENSSL_EXECUTABLE openssl)
        if(QMDP_OPENSSL_EXECUTABLE)
            add_test(
                NAME qsf_tls_gateway
                COMMAND ${Python3_EXECUTABLE}
                ${CMAKE_CURRENT_SOURCE_DIR}/tests/qsf_control/test_qsf_tls_gateway.py
                -v
            )
            set_tests_properties(qsf_tls_gateway PROPERTIES TIMEOUT 45)

            add_test(
                NAME qsunshine_system_auth_protocol
                COMMAND ${Python3_EXECUTABLE}
                    ${CMAKE_CURRENT_SOURCE_DIR}/tests/system_auth/test_system_auth.py
                    -v
            )
            set_tests_properties(qsunshine_system_auth_protocol PROPERTIES TIMEOUT 55)

            # These use real OpenSSL material and a disposable TLS/PAM fixture:
            # login -> VM-bound qsa ticket -> client-owned CSR -> short-lived
            # GameStream client-auth lease.  They intentionally run before a
            # hardware/QEMU E2E gate so a packaging or auth regression has a
            # small, deterministic failure surface.
            add_test(
                NAME qsunshine_gamestream_lease_issuer
                COMMAND ${Python3_EXECUTABLE}
                    ${CMAKE_CURRENT_SOURCE_DIR}/tests/gamestream_auth/test_gamestream_lease.py
                    -v
            )
            set_tests_properties(qsunshine_gamestream_lease_issuer PROPERTIES TIMEOUT 60)

            add_test(
                NAME qsunshine_gamestream_authd_lease
                COMMAND ${Python3_EXECUTABLE}
                    ${CMAKE_CURRENT_SOURCE_DIR}/tests/gamestream_auth/test_authd_lease_endpoint.py
                    -v
            )
            set_tests_properties(qsunshine_gamestream_authd_lease PROPERTIES TIMEOUT 75)
        endif()

    endif()

    add_executable(qmdp_core_tests tests/core_tests.cpp)
    target_link_libraries(qmdp_core_tests PRIVATE qmdp_core Threads::Threads)
    qmdp_target_defaults(qmdp_core_tests)
    add_test(NAME qmdp_core_tests COMMAND qmdp_core_tests)

    if(QMDP_BUILD_DBUS)
        add_library(qmdp_test_support
            tests/support/mock_qemu_dbus_server.cpp
        )
        target_include_directories(qmdp_test_support PUBLIC
            ${CMAKE_CURRENT_SOURCE_DIR}/src
            ${CMAKE_CURRENT_SOURCE_DIR}/tests
        )
        target_link_libraries(qmdp_test_support PUBLIC
            qmdp_dbus qmdp_core Threads::Threads ${QMDP_SYSTEMD_LIBRARY}
        )
        target_compile_definitions(qmdp_test_support PRIVATE _GNU_SOURCE)
        qmdp_target_defaults(qmdp_test_support)

        add_executable(qmdp_dbus_integration_tests
            tests/dbus_integration_tests.cpp
        )
        target_include_directories(qmdp_dbus_integration_tests PRIVATE
            ${CMAKE_CURRENT_SOURCE_DIR}/tests
        )
        target_link_libraries(qmdp_dbus_integration_tests PRIVATE
            qmdp_dbus qmdp_pipeline qmdp_software qmdp_test_support
            Threads::Threads
        )
        qmdp_target_defaults(qmdp_dbus_integration_tests)
        add_test(NAME qmdp_dbus_integration_tests
                 COMMAND qmdp_dbus_integration_tests)
        set_tests_properties(qmdp_dbus_integration_tests PROPERTIES TIMEOUT 15)

        if(QMDP_BUILD_TOOLS)
            add_executable(qmdp_dbus_selftest tools/qmdp_dbus_selftest.cpp)
            target_include_directories(qmdp_dbus_selftest PRIVATE
                ${CMAKE_CURRENT_SOURCE_DIR}/tests
            )
            target_link_libraries(qmdp_dbus_selftest PRIVATE
                qmdp_dbus qmdp_pipeline qmdp_software qmdp_test_support
                Threads::Threads
            )
            qmdp_target_defaults(qmdp_dbus_selftest)

            find_program(QMDP_FFMPEG_EXECUTABLE ffmpeg)
            if(QMDP_FFMPEG_EXECUTABLE)
                add_test(
                    NAME qmdp_no_gpu_ffmpeg_selftest
                    COMMAND qmdp_dbus_selftest
                        --output ${CMAKE_BINARY_DIR}/ctest-selftest
                        --duration-ms 800
                )
                set_tests_properties(qmdp_no_gpu_ffmpeg_selftest
                    PROPERTIES TIMEOUT 20)
            endif()

            find_program(QMDP_DBUS_RUN_SESSION_EXECUTABLE dbus-run-session)
            if(QMDP_DBUS_RUN_SESSION_EXECUTABLE AND
               TARGET qmdp-fake-qemu AND TARGET qemu-display-probe)
                add_test(
                    NAME qmdp_message_bus_map_e2e
                    COMMAND ${CMAKE_CURRENT_SOURCE_DIR}/tests/run_message_bus_e2e.sh
                        $<TARGET_FILE:qmdp-fake-qemu>
                        $<TARGET_FILE:qemu-display-probe>
                        ${CMAKE_BINARY_DIR}/message-bus-map-e2e
                        map
                )
                set_tests_properties(qmdp_message_bus_map_e2e
                    PROPERTIES TIMEOUT 20)

                add_test(
                    NAME qmdp_message_bus_inline_e2e
                    COMMAND ${CMAKE_CURRENT_SOURCE_DIR}/tests/run_message_bus_e2e.sh
                        $<TARGET_FILE:qmdp-fake-qemu>
                        $<TARGET_FILE:qemu-display-probe>
                        ${CMAKE_BINARY_DIR}/message-bus-inline-e2e
                        inline
                )
                set_tests_properties(qmdp_message_bus_inline_e2e
                    PROPERTIES TIMEOUT 20)
            endif()

            if(QMDP_ENABLE_REAL_QEMU_E2E AND TARGET qemu-display-probe)
                find_program(QMDP_QEMU_SYSTEM_X86_64_EXECUTABLE qemu-system-x86_64)
                find_program(QMDP_AS_EXECUTABLE as)
                find_program(QMDP_LD_EXECUTABLE ld)
                find_program(QMDP_FFPROBE_EXECUTABLE ffprobe)
                if(QMDP_DBUS_RUN_SESSION_EXECUTABLE AND
                   QMDP_QEMU_SYSTEM_X86_64_EXECUTABLE AND
                   QMDP_AS_EXECUTABLE AND
                   QMDP_LD_EXECUTABLE AND
                   QMDP_FFPROBE_EXECUTABLE)
                    add_test(
                        NAME qmdp_real_qemu_e2e
                        COMMAND ${CMAKE_CURRENT_SOURCE_DIR}/tests/run_real_qemu_e2e.sh
                            $<TARGET_FILE:qemu-display-probe>
                            ${CMAKE_BINARY_DIR}/real-qemu-e2e
                            ${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures/qmdp_vga_smoke.S
                            relative
                            ${QMDP_QEMU_SYSTEM_X86_64_EXECUTABLE}
                            ${QMDP_AS_EXECUTABLE}
                            ${QMDP_LD_EXECUTABLE}
                            ${QMDP_FFPROBE_EXECUTABLE}
                    )
                    set_tests_properties(qmdp_real_qemu_e2e PROPERTIES TIMEOUT 30)

                    add_test(
                        NAME qmdp_real_qemu_absolute_e2e
                        COMMAND ${CMAKE_CURRENT_SOURCE_DIR}/tests/run_real_qemu_e2e.sh
                            $<TARGET_FILE:qemu-display-probe>
                            ${CMAKE_BINARY_DIR}/real-qemu-absolute-e2e
                            ${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures/qmdp_vga_smoke.S
                            absolute
                            ${QMDP_QEMU_SYSTEM_X86_64_EXECUTABLE}
                            ${QMDP_AS_EXECUTABLE}
                            ${QMDP_LD_EXECUTABLE}
                            ${QMDP_FFPROBE_EXECUTABLE}
                    )
                    set_tests_properties(qmdp_real_qemu_absolute_e2e
                        PROPERTIES TIMEOUT 30)

                endif()
            endif()
        endif()
    endif()
endif()
