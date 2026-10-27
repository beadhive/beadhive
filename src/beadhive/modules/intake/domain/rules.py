"""Pure intake decisions over the domain records. Standard library only."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from .models import DuplicatePair
from .vocabulary import is_untriaged_intake

#: The issue types a cross-hive report may be filed as; bd owns the full type vocabulary, intake
#: gates the user-facing surface to these.
REPORT_TYPES = frozenset({"bug", "feature", "chore"})


def dupes_touching(pairs: Iterable[DuplicatePair], ids: Iterable[str]) -> tuple[DuplicatePair, ...]:
    """The subset of ``pairs`` where either side is one of ``ids``: a duplicate involving a bead
    we care about (a fresh report on entry, or the intake queue at triage)."""
    wanted = set(ids)
    return tuple(p for p in pairs if p.first in wanted or p.second in wanted)


def may_disposition(labels: Iterable[str] | None) -> bool:
    """A disposition only ever fields an untriaged intake bead, never one already fielded."""
    return is_untriaged_intake(tuple(labels or ()))


def _stamp(revision: str) -> tuple[datetime | None, str]:
    """``(updated_at, version)`` of an opaque revision token ``<iso8601>[#<version>]``."""
    moment, _, version = revision.partition("#")
    try:
        return datetime.fromisoformat(moment), version
    except ValueError:
        return None, revision


def revision_is_newer(stored: str, observed: str) -> bool:
    """Whether the ``observed`` issue revision should replace the ``stored`` one on re-import.

    A token is the tracker's own ``updated_at`` (never the local clock) plus its optional version,
    ``<iso8601>[#<version>]``. An equal token, or one older than the stored (a stale read), is not
    newer: re-import repeats are no-ops. A stored token that cannot be read, or an observed one
    that cannot be compared, is treated as changed so the snapshot is refreshed rather than
    frozen. At the same instant a different version is newer."""
    if observed == stored:
        return False
    stored_at, stored_version = _stamp(stored)
    observed_at, observed_version = _stamp(observed)
    if stored_at is None or observed_at is None:
        return True
    try:
        if observed_at != stored_at:
            return observed_at > stored_at
    except TypeError:  # one aware, one naive: not comparable, so refresh
        return True
    return observed_version != stored_version


__all__ = ["REPORT_TYPES", "dupes_touching", "may_disposition", "revision_is_newer"]
