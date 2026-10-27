"""Pure intake decisions over the domain records. Standard library only."""

from __future__ import annotations

from collections.abc import Iterable

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


__all__ = ["REPORT_TYPES", "dupes_touching", "may_disposition"]
