cmake_minimum_required(VERSION 3.16)
project(qsm_qt_input_geometry LANGUAGES CXX)
set(QSM_QT_MAJOR "5" CACHE STRING "Qt major version of the target applications")
if(NOT QSM_QT_MAJOR MATCHES "^[56]$")
    message(FATAL_ERROR "QSM_QT_MAJOR must be 5 or 6")
endif()
if(QSM_QT_MAJOR STREQUAL "5")
    find_package(Qt5 5.15 REQUIRED COMPONENTS Core Gui DBus Widgets)
else()
    find_package(Qt6 6.4 REQUIRED COMPONENTS Core Gui DBus Widgets)
endif()
set(CMAKE_AUTOMOC ON)
add_library(qsm-input-geometry MODULE qsm-input-geometry.cpp)
target_compile_features(qsm-input-geometry PRIVATE cxx_std_17)
target_link_libraries(qsm-input-geometry PRIVATE
    Qt${QSM_QT_MAJOR}::Core Qt${QSM_QT_MAJOR}::Gui Qt${QSM_QT_MAJOR}::DBus Qt${QSM_QT_MAJOR}::Widgets)
set_target_properties(qsm-input-geometry PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/generic")
install(TARGETS qsm-input-geometry LIBRARY DESTINATION generic)
include(CTest)
if(BUILD_TESTING)
    add_executable(qsm-qt-geometry-lifetime lifetime-test.cpp)
    target_link_libraries(qsm-qt-geometry-lifetime PRIVATE Qt${QSM_QT_MAJOR}::Widgets)
    target_compile_features(qsm-qt-geometry-lifetime PRIVATE cxx_std_17)
    foreach(kind gui widgets)
        add_test(NAME qsm_qt_geometry_${kind}_shutdown
            COMMAND ${CMAKE_COMMAND} -E env QT_QPA_PLATFORM=offscreen
                QT_PLUGIN_PATH=${CMAKE_BINARY_DIR} QT_QPA_GENERIC_PLUGINS=qsm-input-geometry
                $<TARGET_FILE:qsm-qt-geometry-lifetime> ${kind})
        set_tests_properties(qsm_qt_geometry_${kind}_shutdown PROPERTIES
            TIMEOUT 10 PASS_REGULAR_EXPRESSION "QSM_QT_GEOMETRY_LIFETIME_OK"
            FAIL_REGULAR_EXPRESSION "No such plugin")
    endforeach()
endif()
