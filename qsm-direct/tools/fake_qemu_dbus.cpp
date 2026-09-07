#include "testing/fake_qemu_service.hpp"

#include <cstdlib>
#include <exception>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>

namespace {

std::uint32_t parse_u32(std::string_view text, std::string_view option) {
    std::size_t consumed = 0U;
    const auto value = std::stoull(std::string(text), &consumed, 10);
    if (consumed != text.size() || value > 0xFFFF'FFFFULL) {
        throw std::invalid_argument(std::string(option) + " has an invalid value");
    }
    return static_cast<std::uint32_t>(value);
}

void usage(const char *program) {
    std::cerr
        << "Usage: " << program << " [options]\n"
        << "  --bus-address ADDRESS   D-Bus address (default DBUS_SESSION_BUS_ADDRESS)\n"
        << "  --name NAME             service name (default org.qemu)\n"
        << "  --width N --height N    synthetic display size\n"
        << "  --frames N --fps N      stream length and cadence\n"
        << "  --inline                send Scanout/Update instead of ScanoutMap/UpdateMap\n";
}

}  // namespace

int main(int argc, char **argv) {
    try {
        qmdp::testing::FakeQemuOptions options;
        if (const char *address = std::getenv("DBUS_SESSION_BUS_ADDRESS")) {
            options.bus_address = address;
        }

        for (int index = 1; index < argc; ++index) {
            const std::string_view argument(argv[index]);
            const auto require_value = [&](std::string_view name) -> std::string_view {
                if (++index >= argc) {
                    throw std::invalid_argument(std::string(name) + " requires a value");
                }
                return argv[index];
            };
            if (argument == "--bus-address") {
                options.bus_address = require_value(argument);
            } else if (argument == "--name") {
                options.bus_name = require_value(argument);
            } else if (argument == "--width") {
                options.width = parse_u32(require_value(argument), argument);
            } else if (argument == "--height") {
                options.height = parse_u32(require_value(argument), argument);
            } else if (argument == "--frames") {
                options.frames = parse_u32(require_value(argument), argument);
            } else if (argument == "--fps") {
                options.fps = parse_u32(require_value(argument), argument);
            } else if (argument == "--inline") {
                options.use_shared_map = false;
            } else if (argument == "--help" || argument == "-h") {
                usage(argv[0]);
                return EXIT_SUCCESS;
            } else {
                throw std::invalid_argument("unknown argument: " + std::string(argument));
            }
        }

        qmdp::testing::FakeQemuService service(std::move(options));
        const int result = service.run();
        const auto stats = service.stats();
        std::cout << "FAKE_QEMU_RESULT"
                  << " frames=" << stats.frames_sent
                  << " ui_info=" << stats.ui_info_calls
                  << " keyboard=" << stats.keyboard_calls
                  << " keyboard_press=" << stats.keyboard_presses
                  << " keyboard_release=" << stats.keyboard_releases
                  << " mouse=" << stats.mouse_calls
                  << " button_press=" << stats.button_presses
                  << " button_release=" << stats.button_releases
                  << " wheel_up=" << stats.wheel_up_clicks
                  << " wheel_down=" << stats.wheel_down_clicks
                  << " absolute=" << (stats.has_absolute_position ? 1 : 0) << ':'
                  << stats.last_absolute_x << 'x' << stats.last_absolute_y
                  << " relative=" << (stats.has_relative_motion ? 1 : 0) << ':'
                  << stats.last_relative_dx << 'x' << stats.last_relative_dy
                  << " requested=" << stats.requested_width << 'x'
                  << stats.requested_height
                  << " listener=" << (stats.listener_registered ? 1 : 0)
                  << " peer_completed=" << (stats.peer_completed ? 1 : 0)
                  << std::endl;
        return result;
    } catch (const std::exception& ex) {
        std::cerr << "fake_qemu_dbus: " << ex.what() << '\n';
        usage(argv[0]);
        return EXIT_FAILURE;
    }
}
