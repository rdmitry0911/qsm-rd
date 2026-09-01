#include "core/session.hpp"

namespace qmdp {

SessionState SessionStateMachine::state() const noexcept {
    return state_;
}

bool SessionStateMachine::dispatch(SessionEvent event) noexcept {
    if (event == SessionEvent::error) {
        state_ = SessionState::failed;
        return true;
    }
    if (event == SessionEvent::reset && state_ == SessionState::failed) {
        state_ = SessionState::idle;
        return true;
    }
    if (event == SessionEvent::stop && state_ != SessionState::idle &&
        state_ != SessionState::stopping) {
        state_ = SessionState::stopping;
        return true;
    }
    if (event == SessionEvent::stopped && state_ == SessionState::stopping) {
        state_ = SessionState::idle;
        return true;
    }

    switch (state_) {
        case SessionState::idle:
            if (event == SessionEvent::prepare) {
                state_ = SessionState::preparing;
                return true;
            }
            break;
        case SessionState::preparing:
            if (event == SessionEvent::prepared) {
                state_ = SessionState::streaming;
                return true;
            }
            break;
        case SessionState::streaming:
            if (event == SessionEvent::resize_begin) {
                state_ = SessionState::reconfiguring;
                return true;
            }
            if (event == SessionEvent::transport_lost) {
                state_ = SessionState::reconnecting;
                return true;
            }
            break;
        case SessionState::reconfiguring:
            if (event == SessionEvent::resize_applied) {
                state_ = SessionState::streaming;
                return true;
            }
            if (event == SessionEvent::transport_lost) {
                state_ = SessionState::reconnecting;
                return true;
            }
            break;
        case SessionState::reconnecting:
            if (event == SessionEvent::reconnected) {
                state_ = SessionState::streaming;
                return true;
            }
            break;
        case SessionState::stopping:
        case SessionState::failed:
            break;
    }
    return false;
}

std::string_view SessionStateMachine::name(SessionState state) noexcept {
    switch (state) {
        case SessionState::idle: return "idle";
        case SessionState::preparing: return "preparing";
        case SessionState::streaming: return "streaming";
        case SessionState::reconfiguring: return "reconfiguring";
        case SessionState::reconnecting: return "reconnecting";
        case SessionState::stopping: return "stopping";
        case SessionState::failed: return "failed";
    }
    return "unknown";
}

std::string_view SessionStateMachine::name(SessionEvent event) noexcept {
    switch (event) {
        case SessionEvent::prepare: return "prepare";
        case SessionEvent::prepared: return "prepared";
        case SessionEvent::resize_begin: return "resize_begin";
        case SessionEvent::resize_applied: return "resize_applied";
        case SessionEvent::transport_lost: return "transport_lost";
        case SessionEvent::reconnected: return "reconnected";
        case SessionEvent::stop: return "stop";
        case SessionEvent::stopped: return "stopped";
        case SessionEvent::error: return "error";
        case SessionEvent::reset: return "reset";
    }
    return "unknown";
}

}  // namespace qmdp
