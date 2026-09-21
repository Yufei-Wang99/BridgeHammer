#!/usr/bin/env bash
# Online controller.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

if [[ ! -x ".venv_ov251/bin/python3" ]]; then
    echo "ERROR: .venv_ov251/bin/python3 missing (need OpenVINO 2025.1)"
    exit 1
fi
if [[ ! -x "eviction/build/eviction_controller" ]]; then
    echo "Building eviction/PCP binaries..."
    make eviction
fi
if [[ ! -f "framework/libfr.so" ]]; then
    make fr
fi

exec .venv_ov251/bin/python3 -m framework.controller "$@"
