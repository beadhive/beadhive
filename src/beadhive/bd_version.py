"""The one rule for reading a ``bd version`` line (bh-1drz, bh-5traf).

Shared by :mod:`beadhive.setup` (the dolt-fix advisory) and :mod:`beadhive.bd_floor` (the claim
floor) so the two cannot grow separate regexes that drift. A leaf module: stdlib only.

THE SUFFIX IS PART OF THE VERSION. Matching only the numeric core and discarding what follows
made the nixpkgs HEAD build, which reports ``bd version 1.1.0 (dev)``, look like a tagged 1.1.0.
The Homebrew HEAD build prints ``HEAD-af076b6``, which has no leading digits and so never matched.
A parenthetical commit hash (``1.3.0 (abc123)``) is still a tagged release; only a between-tags
marker disqualifies.
"""

from __future__ import annotations

import re

# Markers meaning "a build BETWEEN tags", matched as whole words so a commit hash that happens to
# contain the letters (`1.1.0 (abc123rc)`) is not read as a release candidate — a false match here
# SILENCES a real warning, which is the costlier direction to be wrong in.
_NOT_A_TAGGED_RELEASE = re.compile(r"\b(dev|head|dirty|snapshot|pre|rc\d*|alpha|beta)\b", re.I)

_VERSION = re.compile(r"\bbd version (\d+)\.(\d+)\.(\d+)(\S*)")


def tagged_release(version_line: str | None) -> tuple[int, int, int] | None:
    """The ``(major, minor, patch)`` of a TAGGED bd release, or ``None`` when the line is not a
    plain tag: a dev/HEAD/rc build, a suffix welded to the number (``1.1.0-rc.1``,
    ``1.1.0.dev0``, ``1.1.0+build``), or unparseable.

    ``None`` means "cannot judge", never "bad"; each caller decides what that allows."""
    if not version_line:
        return None
    match = _VERSION.search(version_line)
    if not match:
        return None
    if match.group(4):
        return None
    if _NOT_A_TAGGED_RELEASE.search(version_line[match.end() :]):
        return None
    major, minor, patch = (int(g) for g in match.groups()[:3])
    return (major, minor, patch)
