#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

version=${1:?expected version is required}
if [ -n "${UV_EXEC:-}" ]; then
    "${UV_EXEC}" version --no-sync "${version}"
else
    uv version --no-sync "${version}"
fi

# Commitizen includes these tracked registry/client versions in the same bump commit.
uv run --no-sync python scripts/sync-mcp-version.py
