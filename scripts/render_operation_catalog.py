#!/usr/bin/env python3
"""Check that the wire release index.json names as latest carries the live operation catalog.

Published wire releases are immutable, so a stale catalog is never rewritten in place. Without
``--check`` this delegates to ``publish_wire_release.py``, which cuts the next minor release;
the full publish chain is `just wire-publish` (see docs/schemas/wire/README.md).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import publish_wire_release  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="fail when the latest release catalog is stale"
    )
    args = parser.parse_args(argv)
    if not args.check:
        return publish_wire_release.main([])
    root = publish_wire_release.ROOT
    target = publish_wire_release.latest_release_dir(root) / publish_wire_release.CATALOG
    if (
        not target.is_file()
        or target.read_text(encoding="utf-8") != publish_wire_release.render_catalog()
    ):
        parser.error(
            f"{target.relative_to(root)} does not carry the live operation catalog; "
            "publish it with `just wire-publish`"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
