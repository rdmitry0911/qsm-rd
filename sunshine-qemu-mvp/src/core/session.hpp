#pragma once

#include <string_view>

namespace qmdp {

enum class SessionState {
    idle,
    preparing,
    streaming,
    reconfiguring,
    reconnecting,
    stopping,
    failed,
};

enum class SessionEvent {
    prepare,
    prepared,
    resize_begin,
    resize_applied,
    transport_lost,
    reconnected,
    stop,
    stopped,
    error,
    reset,
};

class SessionStateMachine {
public:
    [[nodiscard]] SessionState state() const noexcept;
    [[nodiscard]] bool dispatch(SessionEvent event) noexcept;

    [[nodiscard]] static std::string_view name(SessionState state) noexcept;
    [[nodiscard]] static std::string_view name(SessionEvent event) noexcept;

private:
    SessionState state_ {SessionState::idle};
};

}  // namespace qmdp
