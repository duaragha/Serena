#!/usr/bin/env bash
# Honour the operator's installed NVM default; do not source login-profile hooks.
set -eo pipefail

fleet_repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
fleet_nvm_root="${NVM_DIR:-${HOME}/.nvm}"
if [[ -f "$fleet_nvm_root/nvm.sh" ]]; then
    if ! source "$fleet_nvm_root/nvm.sh" --no-use; then
        echo "Fleet could not load the configured NVM runtime manager" >&2
        exit 1
    fi
    if ! nvm use --silent default; then
        echo "Fleet's configured NVM default is unavailable; install or correct that default before starting" >&2
        exit 1
    fi
fi

if [[ "${1:-}" == "--check" && $# == 1 ]]; then
    if command -v node >/dev/null 2>&1; then
        command -v node
        node --version
    else
        echo "node: unavailable (non-Node workers may still run)"
    fi
    echo "fleet CLI: $fleet_repo_root/.venv/bin/chats"
    exit 0
fi
if [[ $# != 0 ]]; then
    echo "usage: serena-fleet-service.sh [--check]" >&2
    exit 64
fi
exec "$fleet_repo_root/.venv/bin/chats" fleet serve
