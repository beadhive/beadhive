"""Bead-state resolution behind the ``BeadStateLookup`` port (bh-qdezo.5).

Moved off the argv-era ``worktree_inventory.py`` functions of the same shape
(``impl__probe_store``, ``_bead_statuses_for_entry``'s per-id resolution loop, and
``_bead_disposition_relations_for_entry``): the read *shapes* and message texts here match those
functions byte-for-byte, so root's composition swaps only the ``BeadStateLookup`` adapter,
never this policy. Root still owns extracting bead ids from branch names (naming policy) and
resolving each hive's ``main`` clone path (registry lookup) — both stay argv/registry-shaped
root concerns outside this package's boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from ..contracts.state_ports import BeadStateLookup
from .classification import WtDisposition, parse_disposition


def store_reason(lookup: BeadStateLookup, main: Path) -> str:
    """ "" iff the bead store at ``main`` answers with real issues; otherwise WHY it does not.

    The pre-flight's own pre-flight (bh-167s0): ``worktree status`` promises it "repopulates
    fresh metadata before classifying — the pre-flight never uses stale data", so an unreadable
    bead store cannot be silently accepted.  ``probe`` rather than a bead lookup on purpose: the
    question is whether the store answers AT ALL, and a store holding zero issues is the
    measured signature of a schema-fork guard refusing to open the database.  Deliberately NOT
    fatal — an empty store is a legitimate state for a fresh hive, and the caller's job is to
    stop CLASSIFYING confidently, not to refuse to run.
    """
    issues = lookup.probe(main)
    if issues is None:
        return (
            f"the bead store at {main} could not be READ (bd exited non-zero or returned "
            "no JSON) — bd absent, a schema-fork guard refusing to open the database, or a "
            "store engine that is down; try `bh bd list` there to see bd's own error"
        )
    if isinstance(issues, list) and not issues:
        return (
            f"the bead store at {main} answered with ZERO issues — an empty hive, or a store "
            "bd is refusing to read (its schema-fork guard reports no issues rather than an "
            "error: `bd migrate schema --inspect` reports the real version skew)"
        )
    return ""


def bead_states(
    lookup: BeadStateLookup,
    main: Path,
    bead_ids: Sequence[str],
    reason: str,
) -> tuple[dict[str, str], dict[str, str], dict[str, str], str]:
    """Resolve status / close_reason for every (already deduped, first-seen-order) id in
    ``bead_ids``. Returns ``(statuses, close_reasons, unknown_reasons, store_reason)`` —
    ``close_reasons`` holds the AGF lifecycle close_reason (e.g. ``"merged"``, ``"molecule
    landed"``); ``unknown_reasons`` / ``reason`` are bh-167s0: an id that does not resolve is
    reported WITH ITS REASON rather than silently becoming an empty status the classifier reads
    as "open". ``reason`` is a hive-wide store-unreadable reason the caller already resolved
    (via :func:`store_reason`) — repeating the same sentence per bead would say nothing extra.
    """
    statuses: dict[str, str] = {}
    close_reasons: dict[str, str] = {}
    unknown_reasons: dict[str, str] = {}
    for bead_id in bead_ids:
        if bead_id in statuses:
            continue
        bead = lookup.show(bead_id, main)
        statuses[bead_id] = (bead or {}).get("status", "")
        close_reasons[bead_id] = (bead or {}).get("close_reason", "")
        if not statuses[bead_id] and not reason:
            # The store answers, and this ONE id is not in it — a retired bead prefix, or the
            # bead was deleted; see worktree_inventory's argv-era docstring for the measured
            # cause this message names.
            unknown_reasons[bead_id] = (
                f"the store answers, but bead {bead_id} is not in it — the branch names an id "
                "that no longer exists (a retired bead prefix leaves every worktree created "
                "under the old one unresolvable), or the bead was deleted"
            )
    return statuses, close_reasons, unknown_reasons, reason


def dispositions_needing_evidence(
    bead_close_reasons: Mapping[str, str],
) -> dict[str, WtDisposition]:
    """Parse ``bead_close_reasons`` down to the ``retained`` / ``superseded`` records whose
    promised dependency edge still needs confirming — no store read.

    Callers use this to decide WHETHER a store read is needed at all before resolving where to
    read it from: an empty result means :func:`disposition_relations` has nothing to do and the
    caller need not resolve a hive's ``main`` clone path just to find that out.
    """
    return {
        bead_id: disposition
        for bead_id, close_reason in bead_close_reasons.items()
        if (disposition := parse_disposition(str(close_reason or ""))) is not None
        and disposition.state != "stale"
    }


def disposition_relations(
    lookup: BeadStateLookup,
    main: Path,
    dispositions: Mapping[str, WtDisposition],
) -> dict[str, frozenset[tuple[str, str]]]:
    """Read the graph edges promised by authoritative terminal-disposition records.

    ``dispositions`` is the (non-empty) result of :func:`dispositions_needing_evidence`.
    Storage uses Beads' existing relation vocabulary and direction: a retained bead points
    *down* to its consumer via ``relates-to``; a replacement points *down* to the old bead via
    ``supersedes``. The pure classifier receives normalized ``(state, citing_bead)`` pairs and
    therefore never needs a database dependency.
    """
    result: dict[str, frozenset[tuple[str, str]]] = {}
    for bead_id, disposition in dispositions.items():
        if disposition.state == "retained":
            source_id, target_id, relation_type = (
                bead_id,
                disposition.citing_bead,
                "relates-to",
            )
        else:
            source_id, target_id, relation_type = (
                disposition.citing_bead,
                bead_id,
                "supersedes",
            )
        source = lookup.show(source_id, main) or {}
        dependencies = source.get("dependencies") or []
        matched = any(
            isinstance(dep, dict)
            and str(dep.get("type") or dep.get("dependency_type") or "") == relation_type
            and str(dep.get("depends_on_id") or dep.get("id") or "") == target_id
            for dep in dependencies
        )
        if matched:
            result[bead_id] = frozenset({(disposition.state, disposition.citing_bead)})
    return result


__all__ = [
    "bead_states",
    "disposition_relations",
    "dispositions_needing_evidence",
    "store_reason",
]
