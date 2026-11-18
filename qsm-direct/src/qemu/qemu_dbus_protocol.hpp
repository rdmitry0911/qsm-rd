#pragma once

#include "core/ui_info_result.hpp"

#include <cstdint>
#include <string>
#include <string_view>

namespace qmdp::qemu_dbus {

inline constexpr char root_path[] = "/org/qemu/Display1";
inline constexpr char listener_path[] = "/org/qemu/Display1/Listener";
inline constexpr char console_interface[] = "org.qemu.Display1.Console";
inline constexpr char keyboard_interface[] = "org.qemu.Display1.Keyboard";
inline constexpr char mouse_interface[] = "org.qemu.Display1.Mouse";
inline constexpr char listener_interface[] = "org.qemu.Display1.Listener";
inline constexpr char listener_map_interface[] =
    "org.qemu.Display1.Listener.Unix.Map";
inline constexpr char listener_dmabuf2_interface[] =
    "org.qemu.Display1.Listener.Unix.ScanoutDMABUF2";
inline constexpr char properties_interface[] = "org.freedesktop.DBus.Properties";
inline constexpr char introspectable_interface[] =
    "org.freedesktop.DBus.Introspectable";

[[nodiscard]] inline std::string console_path(std::uint32_t console_id) {
    return std::string(root_path) + "/Console_" + std::to_string(console_id);
}

// Display1 error names, as QEMU registers them in ui/dbus-error.c.
inline constexpr char error_failed[] = "org.qemu.Display1.Error.Failed";
inline constexpr char error_invalid[] = "org.qemu.Display1.Error.Invalid";
inline constexpr char error_unsupported[] = "org.qemu.Display1.Error.Unsupported";

// SetUIInfo outcome by error name; the reply's message text is only for
// people.  QEMU replies Unsupported for a console without ui_info (for
// example Standard VGA).  A name outside the three Display1 errors is a
// transport or peer failure, which a later request may not meet again.
[[nodiscard]] inline UiInfoRefusal classify_ui_info_error(std::string_view name) noexcept {
    if (name == error_invalid) { return UiInfoRefusal::invalid; }
    if (name == error_unsupported) { return UiInfoRefusal::unsupported; }
    return UiInfoRefusal::failed;
}

inline constexpr char listener_introspection_xml[] = R"xml(
<node>
  <interface name="org.qemu.Display1.Listener">
    <method name="Scanout"><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="ay" direction="in"/></method>
    <method name="Update"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="ay" direction="in"/></method>
    <method name="ScanoutDMABUF"><arg type="h" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="t" direction="in"/><arg type="b" direction="in"/></method>
    <method name="UpdateDMABUF"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="Disable"/>
    <method name="MouseSet"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="CursorDefine"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="ay" direction="in"/></method>
    <property name="Interfaces" type="as" access="read"/>
  </interface>
  <interface name="org.qemu.Display1.Listener.Unix.Map">
    <method name="ScanoutMap"><arg type="h" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/></method>
    <method name="UpdateMap"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  </interface>
</node>
)xml";

}  // namespace qmdp::qemu_dbus
