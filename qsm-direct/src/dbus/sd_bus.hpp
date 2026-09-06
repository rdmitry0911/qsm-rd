#pragma once

#if __has_include(<systemd/sd-bus.h>) && !defined(QMDP_FORCE_SD_BUS_COMPAT)
#include <systemd/sd-bus.h>
#else
#include "compat/sd_bus_compat.h"
#endif

#include "core/unix_fd.hpp"

#include <chrono>
#include <cstdint>
#include <string>
#include <string_view>

namespace qmdp::dbus {

[[noreturn]] void throw_sd_bus_error(int result,
                                     std::string_view operation,
                                     const sd_bus_error *error = nullptr);
void check(int result,
           std::string_view operation,
           const sd_bus_error *error = nullptr);

class Error {
public:
    Error() = default;
    ~Error();
    Error(const Error&) = delete;
    Error& operator=(const Error&) = delete;

    [[nodiscard]] sd_bus_error *get() noexcept { return &value_; }
    [[nodiscard]] const sd_bus_error& value() const noexcept { return value_; }

private:
    sd_bus_error value_ {};
};

class Message {
public:
    Message() noexcept = default;
    explicit Message(sd_bus_message *message) noexcept : message_(message) {}
    ~Message();
    Message(const Message&) = delete;
    Message& operator=(const Message&) = delete;
    Message(Message&& other) noexcept;
    Message& operator=(Message&& other) noexcept;

    [[nodiscard]] sd_bus_message *get() const noexcept { return message_; }
    [[nodiscard]] sd_bus_message **put() noexcept;
    [[nodiscard]] explicit operator bool() const noexcept { return message_ != nullptr; }
    void reset(sd_bus_message *replacement = nullptr) noexcept;

private:
    sd_bus_message *message_ {};
};

class Slot {
public:
    Slot() noexcept = default;
    ~Slot();
    Slot(const Slot&) = delete;
    Slot& operator=(const Slot&) = delete;
    Slot(Slot&& other) noexcept;
    Slot& operator=(Slot&& other) noexcept;

    [[nodiscard]] sd_bus_slot **put() noexcept;
    void reset(sd_bus_slot *replacement = nullptr) noexcept;

private:
    sd_bus_slot *slot_ {};
};

class Bus {
public:
    Bus() = default;
    ~Bus();
    Bus(const Bus&) = delete;
    Bus& operator=(const Bus&) = delete;
    Bus(Bus&& other) noexcept;
    Bus& operator=(Bus&& other) noexcept;

    static Bus p2p_client_fd(UniqueFd fd);
    static Bus p2p_server_fd(UniqueFd fd, bool allow_anonymous = false);
    static Bus connect_address(std::string_view address,
                               bool bus_client,
                               std::chrono::microseconds timeout);
    static Bus user_bus(std::chrono::microseconds timeout);

    void start();
    void close() noexcept;
    [[nodiscard]] sd_bus *get() const noexcept { return bus_; }
    [[nodiscard]] explicit operator bool() const noexcept { return bus_ != nullptr; }

    // Process everything currently queued, then wait up to timeout for more.
    // Returns false when the connection has gone away.
    bool pump_once(std::chrono::microseconds timeout);

private:
    explicit Bus(sd_bus *bus, UniqueFd transport_fd = {}) noexcept
        : bus_(bus), transport_fd_(std::move(transport_fd)) {}

    sd_bus *bus_ {};
    UniqueFd transport_fd_;
};

[[nodiscard]] std::string describe_error(int result,
                                         const sd_bus_error *error = nullptr);

}  // namespace qmdp::dbus
