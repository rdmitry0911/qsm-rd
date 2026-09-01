#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Apply generation-bound QSF connection profiles in a VirGL guest.
#
# The static qsf_guest_agent deliberately has no compositor dependency.  This
# small adapter is the other half of the contract: it invokes a trusted,
# guest-local apply program and publishes connection-profile-applied only
# after that program has proved the actual desktop scanout.  It never evals a
# profile field or treats any field as a pathname.

set -eu

state_dir=/var/lib/qsf
apply_command=
poll_seconds=1
once=0

usage() {
    echo "Usage: $0 --apply-command /absolute/path [--state-dir DIRECTORY] [--poll-seconds N] [--once]" >&2
    exit 2
}

fail() {
    echo "qsf-virgl-display-adapter: $*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --state-dir)
            [ "$#" -ge 2 ] || usage
            state_dir=$2
            shift 2
            ;;
        --apply-command)
            [ "$#" -ge 2 ] || usage
            apply_command=$2
            shift 2
            ;;
        --poll-seconds)
            [ "$#" -ge 2 ] || usage
            poll_seconds=$2
            shift 2
            ;;
        --once)
            once=1
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            usage
            ;;
    esac
done

case "$state_dir" in
    /*) ;;
    *) fail "--state-dir must be an absolute path" ;;
esac
case "$apply_command" in
    /*) ;;
    *) fail "--apply-command must be an absolute executable path" ;;
esac
case "$poll_seconds" in
    ''|*[!0-9]*) fail "--poll-seconds must be a positive integer" ;;
esac
[ "$poll_seconds" -ge 1 ] || fail "--poll-seconds must be a positive integer"
[ -d "$state_dir" ] || fail "state directory does not exist: $state_dir"
[ -x "$apply_command" ] || fail "apply command is not executable: $apply_command"

profile_path=$state_dir/connection-profile
applied_path=$state_dir/connection-profile-applied

# Populate global profile_* values only from the canonical six-line schema
# written by qsf_guest_agent protocol v2.  Rejecting noncanonical text keeps a
# local untrusted desktop process from smuggling shell syntax into this
# privileged guest-local adapter.
read_canonical_profile() {
    [ -f "$profile_path" ] || return 1
    [ "$(sed -n '$=' "$profile_path")" = 6 ] || return 1
    profile_version=$(sed -n '1s/^version=//p' "$profile_path")
    profile_generation=$(sed -n '2s/^generation=//p' "$profile_path")
    profile_resolution=$(sed -n '3s/^resolution=//p' "$profile_path")
    profile_fps=$(sed -n '4s/^fps=//p' "$profile_path")
    profile_bitrate_kbps=$(sed -n '5s/^bitrate_kbps=//p' "$profile_path")
    profile_codec=$(sed -n '6s/^video_codec=//p' "$profile_path")
    [ "$profile_version" = 2 ] || return 1
    case "$profile_resolution" in
        *x*x*|x*|*x) return 1 ;;
    esac
    profile_width=${profile_resolution%x*}
    profile_height=${profile_resolution#*x}
    for decimal in "$profile_generation" "$profile_width" "$profile_height" \
                   "$profile_fps" "$profile_bitrate_kbps"; do
        case "$decimal" in
            ''|0*|*[!0-9]*) return 1 ;;
        esac
    done
    [ "$profile_width" -ge 64 ] && [ "$profile_width" -le 16384 ] || return 1
    [ "$profile_height" -ge 64 ] && [ "$profile_height" -le 16384 ] || return 1
    [ "$profile_fps" -ge 10 ] && [ "$profile_fps" -le 240 ] || return 1
    [ "$profile_bitrate_kbps" -ge 500 ] && [ "$profile_bitrate_kbps" -le 500000 ] || return 1
    case "$profile_codec" in
        H264|HEVC|AV1) ;;
        *) return 1 ;;
    esac
    profile_canonical=$state_dir/.connection-profile-canonical.$$
    {
        printf 'version=2\n'
        printf 'generation=%s\n' "$profile_generation"
        printf 'resolution=%sx%s\n' "$profile_width" "$profile_height"
        printf 'fps=%s\n' "$profile_fps"
        printf 'bitrate_kbps=%s\n' "$profile_bitrate_kbps"
        printf 'video_codec=%s\n' "$profile_codec"
    } >"$profile_canonical" || return 1
    if ! cmp -s "$profile_path" "$profile_canonical"; then
        rm -f "$profile_canonical"
        return 1
    fi
    rm -f "$profile_canonical"
    return 0
}

last_applied_generation=
while :; do
    if read_canonical_profile && [ "$profile_generation" != "$last_applied_generation" ]; then
        # Snapshot first.  The command receives this immutable, verified input
        # and must return success only after it sees the actual active scanout
        # at profile_width x profile_height (for Weston, use weston-info or a
        # DRM query after the compositor restart).
        snapshot=$state_dir/.connection-profile-apply.$$
        rm -f "$snapshot"
        {
            printf 'version=2\n'
            printf 'generation=%s\n' "$profile_generation"
            printf 'resolution=%sx%s\n' "$profile_width" "$profile_height"
            printf 'fps=%s\n' "$profile_fps"
            printf 'bitrate_kbps=%s\n' "$profile_bitrate_kbps"
            printf 'video_codec=%s\n' "$profile_codec"
        } >"$snapshot" || fail "cannot snapshot connection profile"
        chmod 600 "$snapshot" || fail "cannot protect profile snapshot"
        # A profile replacement between parsing and snapshotting must not run
        # the old dimensions against the newer profile's bytes.
        if ! cmp -s "$profile_path" "$snapshot"; then
            rm -f "$snapshot"
            sleep "$poll_seconds"
            continue
        fi
        # Keep the prior generation-bound acknowledgement present while the
        # compositor applies this snapshot.  qsf_guest_agent compares the
        # current profile with connection-profile-applied, so removing the
        # latter would turn a valid old ACK into a transient "no ACK" state
        # and can make an unrelated reader retry or time out.  The successful
        # same-directory mv below atomically replaces it with this generation.
        if "$apply_command" "$profile_width" "$profile_height" "$profile_fps" "$snapshot"; then
            # A newer request always wins; do not acknowledge an old snapshot.
            if cmp -s "$profile_path" "$snapshot"; then
                mv -f "$snapshot" "$applied_path" || fail "cannot publish applied profile"
                last_applied_generation=$profile_generation
                echo "qsf-virgl-display-adapter: applied generation=$profile_generation resolution=$profile_resolution" >&2
                [ "$once" = 1 ] && exit 0
            else
                rm -f "$snapshot"
            fi
        else
            rm -f "$snapshot"
            echo "qsf-virgl-display-adapter: apply command failed for generation=$profile_generation" >&2
        fi
    fi
    sleep "$poll_seconds"
done
