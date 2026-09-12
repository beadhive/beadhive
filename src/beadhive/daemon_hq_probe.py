"""Bounded-process entry point for the daemon's read-only local HQ readiness probe."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import hq

_MAX_REQUEST_BYTES = 16_384


def main() -> int:
    raw = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
    if len(raw) > _MAX_REQUEST_BYTES:
        return 2
    try:
        request = json.loads(raw)
        path = request["path"]
    except (KeyError, TypeError, ValueError):
        return 2
    if not isinstance(path, str) or not path:
        return 2
    state = hq.local_readiness(Path(path))
    if state not in {"ready", "hq_not_initialized", "hq_path_unavailable"}:
        return 2
    sys.stdout.write(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
