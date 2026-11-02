"""XTDB version gate: the loader is validated against ``>=2.2.0,<2.3`` only (bh-cntr0)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SUPPORTED_RANGE = ">=2.2.0,<2.3"
MINIMUM = (2, 2, 0)
EXCLUSIVE_MAXIMUM = (2, 3, 0)
VERSION_SQL = "SELECT version()"
_XTDB_VERSION = re.compile(r"XTDB\s+([0-9]+)\.([0-9]+)(?:\.([0-9]+))?([-+][0-9A-Za-z.+-]*)?")


class UnsupportedXtdbVersion(RuntimeError):
    """The server is not an XTDB node this loader is validated against."""


@dataclass(frozen=True)
class XtdbVersion:
    major: int
    minor: int
    patch: int
    suffix: str = ""

    @property
    def release(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}{self.suffix}"


def parse_version(text: str) -> XtdbVersion:
    """Parse ``SELECT version()`` output, e.g. ``PostgreSQL 16 (XTDB 2.2.0)``."""
    match = _XTDB_VERSION.search(text) if isinstance(text, str) else None
    if match is None:
        raise UnsupportedXtdbVersion(
            f"cannot read an XTDB version from {text!r}: the target is not an XTDB node "
            f"(expected 'PostgreSQL 16 (XTDB 2.2.0)'); point the loader at an XTDB "
            f"{SUPPORTED_RANGE} pgwire port"
        )
    major, minor, patch, suffix = match.groups()
    return XtdbVersion(int(major), int(minor), int(patch or 0), suffix or "")


def check_version(text: str) -> XtdbVersion:
    """Return the parsed version when it is in ``>=2.2.0,<2.3``; raise otherwise.

    The message says what to do: an older node must be upgraded to a pinned 2.2.x image, and a
    newer one is untested, so the loader stays on 2.2.x until the conformance kit has been re-run
    against it and the range here is widened on purpose.
    """
    version = parse_version(text)
    older = version.release < MINIMUM or (
        version.release == MINIMUM and version.suffix.startswith("-")
    )
    if older:
        raise UnsupportedXtdbVersion(
            f"XTDB {version} is older than the supported floor 2.2.0. Upgrade the node to a "
            f"pinned 2.2.x image (the operator owns start, stop and upgrade); supported range "
            f"{SUPPORTED_RANGE}"
        )
    if version.release >= EXCLUSIVE_MAXIMUM:
        raise UnsupportedXtdbVersion(
            f"XTDB {version} is newer than the validated range {SUPPORTED_RANGE}. Run the "
            f"loader against a pinned 2.2.x image, or re-run the beadlog conformance kit and the "
            f"integration tests against {version} and widen the range deliberately; its "
            f"transaction and SYSTEM_TIME semantics are not assumed to match"
        )
    return version


def require_supported_version(connection: Any) -> XtdbVersion:
    """Ask the server for ``version()`` and apply :func:`check_version`."""
    row = connection.execute(VERSION_SQL).fetchone()
    return check_version(str(row[0]) if row else "")


__all__ = [
    "EXCLUSIVE_MAXIMUM",
    "MINIMUM",
    "SUPPORTED_RANGE",
    "VERSION_SQL",
    "UnsupportedXtdbVersion",
    "XtdbVersion",
    "check_version",
    "parse_version",
    "require_supported_version",
]
