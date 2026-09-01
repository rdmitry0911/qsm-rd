#include "dbus/sd_bus.hpp"

#include <cerrno>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <system_error>

namespace qmdp::dbus {
namespace {

constexpr std::uint64_t infinite_timeout = std::numeric_limits<std::uint64_t>::max();

std::uint64_t to_usec(std::chrono::microseconds value) {
    if (value.count() < 0) {
        return infinite_timeout;
    }
    return static_cast<std::uint64_t>(value.count());
}

}  // namespace

std::string describe_error(int result, const sd_bus_error *error) {
    std::string message;
    if (error != nullptr && error->name != nullptr) {
        message += error->name;
        if (error->message != nullptr) {
            message += ": ";
            message += error->message;
        }
    } else if (result < 0) {
        message = std::strerror(-result);
    } else {
        message = "unknown sd-bus error";
    }
    return message;
}

[[noreturn]] void throw_sd_bus_error(int result,
                                     std::string_view operation,
                                     const sd_bus_error *error) {
    throw std::runtime_error(std::string(operation) + ": " +
                             describe_error(result, error));
}

void check(int result,
           std::string_view operation,
           const sd_bus_error *error) {
    if (result < 0) {
        throw_sd_bus_error(result, operation, error);
    }
}

Error::~Error() {
    sd_bus_error_free(&value_);
}

Message::~Message() {
    reset();
}

Message::Message(Message&& other) noexcept : message_(other.message_) {
    other.message_ = nullptr;
}

Message& Message::operator=(Message&& other) noexcept {
    if (this != &other) {
        reset(other.message_);
        other.message_ = nullptr;
    }
    return *this;
}

sd_bus_message **Message::put() noexcept {
    reset();
    return &message_;
}

void Message::reset(sd_bus_message *replacement) noexcept {
    if (message_ != nullptr) {
        sd_bus_message_unref(message_);
    }
    message_ = replacement;
}

Slot::~Slot() {
    reset();
}

Slot::Slot(Slot&& other) noexcept : slot_(other.slot_) {
    other.slot_ = nullptr;
}

Slot& Slot::operator=(Slot&& other) noexcept {
    if (this != &other) {
        reset(other.slot_);
        other.slot_ = nullptr;
    }
    return *this;
}

sd_bus_slot **Slot::put() noexcept {
    reset();
    return &slot_;
}

void Slot::reset(sd_bus_slot *replacement) noexcept {
    if (slot_ != nullptr) {
        sd_bus_slot_unref(slot_);
    }
    slot_ = replacement;
}

Bus::~Bus() {
    close();
}

Bus::Bus(Bus&& other) noexcept
    : bus_(other.bus_), transport_fd_(std::move(other.transport_fd_)) {
    other.bus_ = nullptr;
}

Bus& Bus::operator=(Bus&& other) noexcept {
    if (this != &other) {
        close();
        bus_ = other.bus_;
        transport_fd_ = std::move(other.transport_fd_);
        other.bus_ = nullptr;
    }
    return *this;
}

Bus Bus::p2p_client_fd(UniqueFd fd) {
    if (!fd) {
        throw std::invalid_argument("invalid peer D-Bus client fd");
    }
    sd_bus *raw = nullptr;
    check(sd_bus_new(&raw), "sd_bus_new");
    Bus bus(raw, std::move(fd));
    check(sd_bus_set_fd(raw, bus.transport_fd_.get(), bus.transport_fd_.get()),
          "sd_bus_set_fd(client)");
    check(sd_bus_set_bus_client(raw, 0), "sd_bus_set_bus_client(client)");
    check(sd_bus_negotiate_fds(raw, 1), "sd_bus_negotiate_fds(client)");
    return bus;
}

Bus Bus::p2p_server_fd(UniqueFd fd, bool allow_anonymous) {
    if (!fd) {
        throw std::invalid_argument("invalid peer D-Bus server fd");
    }
    sd_bus *raw = nullptr;
    check(sd_bus_new(&raw), "sd_bus_new");
    Bus bus(raw, std::move(fd));
    sd_id128_t id {};
    check(sd_id128_randomize(&id), "sd_id128_randomize");
    check(sd_bus_set_fd(raw, bus.transport_fd_.get(), bus.transport_fd_.get()),
          "sd_bus_set_fd(server)");
    check(sd_bus_set_server(raw, 1, id), "sd_bus_set_server");
    if (allow_anonymous) {
        check(sd_bus_set_anonymous(raw, 1), "sd_bus_set_anonymous");
    }
    check(sd_bus_negotiate_fds(raw, 1), "sd_bus_negotiate_fds(server)");
    return bus;
}

Bus Bus::connect_address(std::string_view address,
                         bool bus_client,
                         std::chrono::microseconds timeout) {
    if (address.empty()) {
        throw std::invalid_argument("D-Bus address is empty");
    }
    sd_bus *raw = nullptr;
    check(sd_bus_new(&raw), "sd_bus_new");
    Bus bus(raw);
    const std::string stable_address(address);
    check(sd_bus_set_address(raw, stable_address.c_str()), "sd_bus_set_address");
    check(sd_bus_set_bus_client(raw, bus_client ? 1 : 0),
          "sd_bus_set_bus_client(address)");
    check(sd_bus_negotiate_fds(raw, 1), "sd_bus_negotiate_fds(address)");
    check(sd_bus_set_method_call_timeout(raw, to_usec(timeout)),
          "sd_bus_set_method_call_timeout");
    return bus;
}

Bus Bus::user_bus(std::chrono::microseconds timeout) {
    sd_bus *raw = nullptr;
    check(sd_bus_open_user(&raw), "sd_bus_open_user");
    Bus bus(raw);
    check(sd_bus_set_method_call_timeout(raw, to_usec(timeout)),
          "sd_bus_set_method_call_timeout");
    return bus;
}

void Bus::start() {
    if (bus_ == nullptr) {
        throw std::logic_error("cannot start an empty D-Bus connection");
    }
    check(sd_bus_start(bus_), "sd_bus_start");
}

void Bus::close() noexcept {
    if (bus_ != nullptr) {
        sd_bus_flush(bus_);
        sd_bus_close(bus_);
        sd_bus_unref(bus_);
        bus_ = nullptr;
    }
    transport_fd_.reset();
}

bool Bus::pump_once(std::chrono::microseconds timeout) {
    if (bus_ == nullptr) {
        return false;
    }
    for (;;) {
        const int processed = sd_bus_process(bus_, nullptr);
        if (processed < 0) {
            if (processed == -ECONNRESET || processed == -ENOTCONN ||
                processed == -EPIPE) {
                return false;
            }
            throw_sd_bus_error(processed, "sd_bus_process");
        }
        if (processed == 0) {
            break;
        }
    }
    const int waited = sd_bus_wait(bus_, to_usec(timeout));
    if (waited < 0) {
        if (waited == -ECONNRESET || waited == -ENOTCONN || waited == -EPIPE) {
            return false;
        }
        throw_sd_bus_error(waited, "sd_bus_wait");
    }
    return true;
}

}  // namespace qmdp::dbus
