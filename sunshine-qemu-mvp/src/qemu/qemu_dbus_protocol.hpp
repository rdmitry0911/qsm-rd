#pragma once

#include <cstdint>
#include <string>

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
