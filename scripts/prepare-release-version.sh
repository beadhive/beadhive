#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

version=${1:?expected version is required}
if [ -n "${UV_EXEC:-}" ]; then
    "${UV_EXEC}" version --no-sync "${version}"
else
    uv version --no-sync "${version}"
fi
python3 scripts/refresh_modularization_closeout.py --write
