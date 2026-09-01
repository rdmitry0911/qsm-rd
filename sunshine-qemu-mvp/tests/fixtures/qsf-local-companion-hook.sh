#!/usr/bin/env bash
# Operation-compatible adapter used to exercise the runner's external
# companion-client hook.  A production mTLS client can replace this executable
# and use its own endpoint/certificate configuration; it must preserve the
# qsf_client operation and stdin/stdout contract.
set -euo pipefail

: "${QSF_CONTROL_SOCKET:?runner did not provide QSF_CONTROL_SOCKET}"
: "${QSF_TOKEN_FILE:?runner did not provide QSF_TOKEN_FILE}"
: "${QSF_CLIENT_BINARY:?runner did not provide QSF_CLIENT_BINARY}"

exec "${QSF_PYTHON_BINARY:-python3}" "$QSF_CLIENT_BINARY" \
  --socket "$QSF_CONTROL_SOCKET" --token-file "$QSF_TOKEN_FILE" "$@"
