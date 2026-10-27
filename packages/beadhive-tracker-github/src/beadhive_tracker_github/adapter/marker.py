"""The own-message marker codec (tracker-adapter-port-adr.md, inconsistency 2).

The adapter appends one footer line to every comment it posts and decodes it on read::

    <!-- bh:v1 hive=<hive> install=<install> outbox=<outbox id> -->

* The encoding is **adapter-owned and versioned** (``bh:v1``); the port carries only the decoded
  :class:`~beadhive.modules.tracker.contracts.OwnMarker`.
* It counts **only as the last non-empty line**, so a quoted copy (``> <!-- bh:v1 ... -->``) or a
  marker followed by more text is ignored, and the body is returned unchanged.
* It is an HTML comment, invisible in rendered Markdown. It is a loop-avoidance and attribution
  aid, **not a security boundary**: anyone can type it. Deciding whether a comment is "own"
  (outbox id, App id, known actor AND marker) is the intake consumer's job, never the adapter's.
* The ``outbox`` id is what lets a comment whose write outcome was unknown be found again.

Pure: no I/O, no clock. GitHub returns web-edited bodies with ``\\r\\n`` line endings, so both
endings decode.
"""

from __future__ import annotations

import re

from beadhive.modules.tracker.contracts import OwnMarker

__all__ = ["MARKER_VERSION", "decode_marker", "encode_marker", "marker_line"]

#: The marker grammar version this adapter writes and reads.
MARKER_VERSION = "bh:v1"

# One field value: no whitespace, no ``-`` run that could close or nest an HTML comment, no ``>``.
_VALUE = r"[A-Za-z0-9._:/@+=-]+"
_SAFE_VALUE = re.compile(_VALUE)
_TRAILER = re.compile(
    rf"(?:\A|\n)[ \t]*<!-- {re.escape(MARKER_VERSION)} hive=(?P<hive>{_VALUE}) "
    rf"install=(?P<install>{_VALUE}) outbox=(?P<outbox>{_VALUE}) -->[ \t]*(?:\r?\n[ \t]*)*\Z"
)


def _checked(name: str, value: str) -> str:
    # Static message: a field value is never repeated.
    if not _SAFE_VALUE.fullmatch(value) or "--" in value:
        raise ValueError(f"marker {name} must be a non-empty token without spaces or '--'")
    return value


def marker_line(marker: OwnMarker) -> str:
    """The footer line for ``marker``; refuses a field that would break the grammar."""
    hive = _checked("hive", marker.hive)
    install = _checked("install", marker.install)
    outbox = _checked("outbox_id", marker.outbox_id)
    return f"<!-- {MARKER_VERSION} hive={hive} install={install} outbox={outbox} -->"


def encode_marker(body: str, marker: OwnMarker) -> str:
    """``body`` with the marker appended as its last line (after one blank line)."""
    line = marker_line(marker)
    text = body.rstrip()
    return f"{text}\n\n{line}" if text else line


def decode_marker(body: str) -> tuple[str, OwnMarker | None]:
    """Split a comment body into ``(text, marker)``.

    Only a well-formed ``bh:v1`` marker on the LAST non-empty line decodes; the returned text is
    everything before it with trailing whitespace removed. Anything else returns ``(body, None)``
    unchanged, whichever hive the marker names (classifying it is the consumer's job)."""
    match = _TRAILER.search(body)
    if match is None:
        return body, None
    marker = OwnMarker(match["hive"], match["install"], match["outbox"])
    if "--" in marker.hive + marker.install + marker.outbox_id:
        return body, None
    return body[: match.start()].rstrip(), marker
