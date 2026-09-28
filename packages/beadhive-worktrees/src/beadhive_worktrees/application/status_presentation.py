"""Pure data shaping shared by every ``bh worktree status`` display path (bh-qdezo.7).

Moved verbatim (byte-identical behavior) from ``worktree_inventory.py``'s ``impl__status_tags``
and ``impl__ordered_statuses``. Printing the text tree itself stays a root concern — this module
only builds the strings and orders the rows a root display function consumes.
"""

from __future__ import annotations

from typing import Any


def status_tags(st: Any) -> str:
    """The trailing tag run appended to one displayed row.

    ``? UNKNOWN`` is deliberately the loudest thing on the line and the only class carrying a
    glyph: it is the one classification that means "do not act on this row", and it has to be
    findable by eye in a tree of thirty (bh-167s0 — "visually distinct in the displayed tree").
    A ``DIRTY`` row also shows what it is masking, so a dirty-but-SAFE seat is distinguishable
    from a dirty-and-open one and a dirty row over an unresolvable bead cannot look ordinary.
    """
    tags = ""
    if st.merged:
        tags += "  merged"
    if st.dirty:
        tags += "  dirty"
    if getattr(st, "underlying", None):
        tags += f"  (under: {str(st.underlying).upper()})"
    if st.safe:
        tags += "  SAFE"
    elif getattr(st, "precious", ()):
        paths = ",".join(f"{item.path}({item.bytes}B)" for item in st.precious)
        tags += f"  precious={paths}"
    if getattr(st, "legacy_root", False):
        tags += "  legacy-root"
    if getattr(st, "disposition_reason", ""):
        tags += f"  reason={st.disposition_reason}"
    if getattr(st, "citing_bead", ""):
        tags += f"  citing={st.citing_bead}"
    if getattr(st, "binding_gaps", ()):
        tags += f"  binding-gap={','.join(st.binding_gaps)}"
    return tags


def ordered_statuses(entries: list, statuses_by_prefix: dict[str, list]) -> list:
    """Flatten completed classifications in managed-repository order, never finish order."""
    return [
        status
        for entry in entries
        for status in statuses_by_prefix.get(str(entry.get("prefix", "")), [])
    ]


__all__ = ["ordered_statuses", "status_tags"]
