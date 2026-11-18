#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace qmdp {

// The answer of a display to one Console.SetUIInfo size request when it did
// not take it.  The values are the wire codes of the worker's record to the
// browser bridge (see encode_ui_info_result).
enum class UiInfoRefusal : std::uint8_t {
    // The display refuses this size (Display1 Error.Invalid).  Another size
    // is a new request.
    invalid = 1U,
    // The display does not follow UI sizes at all (Display1
    // Error.Unsupported, which QEMU itself returns for a console without
    // ui_info).
    unsupported = 2U,
    // A transient or device failure (Display1 Error.Failed, or any other
    // D-Bus error).  Asking again later is legitimate.
    failed = 3U,
};

// Thrown by a display for a SetUIInfo request its peer answered with an
// error reply.  This is the outcome of that request, not a failure of the
// display connection.
class UiInfoError final : public std::runtime_error {
public:
    UiInfoError(UiInfoRefusal kind, std::string name, std::string reason)
        : std::runtime_error("SetUIInfo " + name + (reason.empty() ? "" : ": " + reason)),
          kind_(kind), name_(std::move(name)), reason_(std::move(reason)) {}

    [[nodiscard]] UiInfoRefusal kind() const noexcept { return kind_; }
    // D-Bus error name exactly as the peer replied.
    [[nodiscard]] const std::string& name() const noexcept { return name_; }
    // Human-readable message of the reply; never a basis for behaviour.
    [[nodiscard]] const std::string& reason() const noexcept { return reason_; }

private:
    UiInfoRefusal kind_;
    std::string name_;
    std::string reason_;
};

inline constexpr std::uint8_t ui_info_result_version = 1U;
inline constexpr std::size_t ui_info_result_max_name = 255U;
inline constexpr std::size_t ui_info_result_max_reason = 240U;

// One bounded record for the browser bridge:
//   u8 version, u8 refusal, u32 width, u32 height (network order),
//   u8 name length, name (D-Bus name characters only),
//   u8 reason length, reason (printable ASCII only).
// The text comes from the display peer; it is reduced to characters that a
// status line can show literally and is never interpreted.
[[nodiscard]] inline std::vector<std::uint8_t> encode_ui_info_result(
        const UiInfoError& error, std::uint32_t width, std::uint32_t height) {
    std::string name;
    for (const char character : error.name()) {
        if (name.size() == ui_info_result_max_name) { break; }
        const bool allowed = (character >= 'a' && character <= 'z') ||
            (character >= 'A' && character <= 'Z') ||
            (character >= '0' && character <= '9') || character == '_' || character == '.';
        name.push_back(allowed ? character : '_');
    }
    std::string reason;
    for (const char character : error.reason()) {
        if (reason.size() == ui_info_result_max_reason) { break; }
        const auto byte = static_cast<unsigned char>(character);
        reason.push_back(byte >= 0x20U && byte <= 0x7eU ? character : '?');
    }
    std::vector<std::uint8_t> payload;
    payload.reserve(12U + name.size() + reason.size());
    payload.push_back(ui_info_result_version);
    payload.push_back(static_cast<std::uint8_t>(error.kind()));
    for (const std::uint32_t value : {width, height}) {
        for (int shift = 24; shift >= 0; shift -= 8) {
            payload.push_back(static_cast<std::uint8_t>((value >> shift) & 0xffU));
        }
    }
    payload.push_back(static_cast<std::uint8_t>(name.size()));
    payload.insert(payload.end(), name.begin(), name.end());
    payload.push_back(static_cast<std::uint8_t>(reason.size()));
    payload.insert(payload.end(), reason.begin(), reason.end());
    return payload;
}

// Worker-side policy for the browser's size requests to one display.
//
// An error reply of the display is the terminal answer to that request:
// it is returned so the browser learns it, and it never stops the input
// receiver.  Unsupported ends size requests for the session; later requests
// are answered from the recorded reply without asking the display again.
// Any other exception keeps the former behaviour: with
// retain_fixed_scanout the display keeps its scanout and further requests
// are dropped silently, otherwise the exception propagates.
class UiInfoRequests {
public:
    explicit UiInfoRequests(bool retain_fixed_scanout) noexcept
        : retain_fixed_scanout_(retain_fixed_scanout) {}

    template <typename Apply>
    [[nodiscard]] std::optional<UiInfoError> request(Apply&& apply) {
        if (!supported_) { return unsupported_; }
        try {
            std::forward<Apply>(apply)();
        } catch (const UiInfoError& error) {
            if (error.kind() == UiInfoRefusal::unsupported) {
                supported_ = false;
                unsupported_ = error;
            }
            return error;
        } catch (...) {
            if (!retain_fixed_scanout_) { throw; }
            supported_ = false;
        }
        return std::nullopt;
    }

    [[nodiscard]] bool supported() const noexcept { return supported_; }

private:
    bool retain_fixed_scanout_ {false};
    bool supported_ {true};
    std::optional<UiInfoError> unsupported_;
};

}  // namespace qmdp
