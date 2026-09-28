"""Root-supplied adapters over Beads, bd argv, claim records, and gh PR lookups.

The concrete ``BeadStateLookup`` / ``ClaimRecords`` / ``MergeEvidence`` ports
(``beadhive_worktrees.contracts``) the worktree safety classifier's policy depends on
(bh-qdezo.5). Composition, never policy: each method is a thin, behavior-preserving
pass-through to an existing root boundary, so swapping any one of them never touches
classification logic.  Bead-state reads prefer the supervised ``BeadsSession`` route and select
the argv adapter before execution only when ``work.beads.route`` permits that named fallback.

Every method resolves its collaborator (``bd``, ``claim_authority``, ``ghpr``) at call time
rather than caching a bound reference, so the existing per-module patch seams
(``worktree_inventory.bd``, ``worktree.bd``, ...) keep intercepting these calls unchanged —
they are all the same shared module object.

:data:`BEAD_STATE_LOOKUP`, :data:`CLAIM_RECORDS`, and :data:`MERGE_EVIDENCE` are the composed
port instances (bh-qdezo.9). Every root consumer reads them from this module at call time, so
they are the one consumer-boundary substitution point: a higher-layer test swaps a port here
(for example with ``beadhive_worktrees.testing.InMemoryBeadStateLookup``) instead of patching a
worktree module's private helpers or the ``bd`` module reached through its namespace.
"""

from __future__ import annotations

import importlib
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from beadhive_worktrees import BeadStateLookup, ClaimRecords, MergeEvidence

from . import bd, bd_cli, beads_routing, ghpr, registry
from .config_consumer_ports import work_settings as config

_GET_CAPABILITIES = frozenset({"issues.get"})
_LIST_CAPABILITIES = frozenset({"issues.list"})


def _core() -> Any:
    """Resolve the workspace package lazily, preserving root's package boundary."""
    return importlib.import_module("beadhive_core")


def _client() -> Any:
    """Resolve Beads wire errors lazily for the same root/package boundary."""
    return importlib.import_module("beadhive_beads_client")


def _entry(main: Path) -> Any:
    return registry.entry_for_dir(config.load(), main)


def session_factory(main: Path, entry: Any, capabilities: frozenset[str]) -> Any:
    """The supervised, unopened session selected by the shared root routing seam."""
    return beads_routing.hive_session(main, entry, capabilities)


class ArgvBeadStateLookup:
    """``BeadStateLookup`` over ``bd.json`` / ``bd.show`` subprocess reads."""

    def probe(self, main: Path) -> list[Any] | None:
        return bd_cli.routes(str(main)).issue_list()

    def show(self, bead_id: str, main: Path) -> dict[str, Any] | None:
        return bd.show(bead_id, str(main))

    def all_issues(self, main: Path) -> list[Any] | None:
        return bd_cli.routes(str(main)).issue_list(all_=True, include_infra=True, limit=0)


class BeadsSessionBeadStateLookup:
    """``BeadStateLookup`` over one already-open, context-verified ``BeadsSession``.

    Calls use the same operation names and routing table as the other API-ready work cohorts.
    Conversion is type erasure only: the generated models' ``to_dict`` output is the ``bd`` JSON
    shape consumed by :mod:`beadhive_worktrees.policy.bead_state`.
    """

    def __init__(self, session: Any) -> None:
        self._session = session

    def probe(self, _main: Path) -> list[Any] | None:
        page = (
            _core()
            .default_table()
            .call_api(
                self._session,
                "work.issue.list",
                limit=1,
            )
        )
        return [item.to_dict() for item in page.items]

    def show(self, bead_id: str, _main: Path) -> dict[str, Any] | None:
        try:
            issue = (
                _core()
                .default_table()
                .call_api(
                    self._session,
                    "work.issue.get",
                    bead_id,
                )
            )
        except _client().ServiceProblem as exc:
            if exc.problem.code == "not_found":
                return None
            raise
        return issue.to_dict()

    def all_issues(self, _main: Path) -> list[Any] | None:
        page = (
            _core()
            .default_table()
            .call_api(
                self._session,
                "work.issue.list",
                limit=0,
                all_=True,
                include_infra=True,
            )
        )
        return [item.to_dict() for item in page.items]


class RoutedBeadStateLookup:
    """Select API or argv once, before each ``BeadStateLookup`` read begins.

    Opening the supervised session is the selection boundary.  A missing service/capability may
    choose ``ArgvBeadStateLookup`` only through :func:`beads_routing.allow_cli_route`; after an
    API session opens, a failed read is never replayed through ``bd``.
    """

    def __init__(self, fallback: BeadStateLookup | None = None) -> None:
        self._fallback = fallback or ArgvBeadStateLookup()

    def _read(
        self,
        main: Path,
        capabilities: frozenset[str],
        method: str,
        *args: Any,
    ) -> Any:
        entry = _entry(main)
        with ExitStack() as stack:
            try:
                session = stack.enter_context(session_factory(main, entry, capabilities))
            except (*beads_routing.unavailable_errors(), OSError, ValueError) as exc:
                beads_routing.allow_cli_route(entry, exc)
                return getattr(self._fallback, method)(*args, main)
            return getattr(BeadsSessionBeadStateLookup(session), method)(*args, main)

    def probe(self, main: Path) -> list[Any] | None:
        return self._read(main, _LIST_CAPABILITIES, "probe")

    def show(self, bead_id: str, main: Path) -> dict[str, Any] | None:
        return self._read(main, _GET_CAPABILITIES, "show", bead_id)

    def all_issues(self, main: Path) -> list[Any] | None:
        return self._read(main, _LIST_CAPABILITIES, "all_issues")


class ClaimAuthorityRecords:
    """``ClaimRecords`` over ``claim_authority``'s record-path bookkeeping."""

    def record_path(self, target: Any) -> Path | None:
        from . import claim_authority

        return claim_authority.record_path(target)

    def remove_record_path(self, path: Path | None) -> None:
        from . import claim_authority

        claim_authority.remove_record_path(path)


class GhprMergeEvidence:
    """``MergeEvidence`` over ``ghpr.merged_pr_for`` (``gh pr list --state merged --head``)."""

    def merged_pr(self, entry: Any, branch: str) -> Any | None:
        return ghpr.merged_pr_for(entry, branch)


#: The composed ``BeadStateLookup``: supervised Beads first, named argv compatibility fallback.
BEAD_STATE_LOOKUP: BeadStateLookup = RoutedBeadStateLookup()
#: The composed ``ClaimRecords`` over ``claim_authority``'s record paths.
CLAIM_RECORDS: ClaimRecords = ClaimAuthorityRecords()
#: The composed ``MergeEvidence`` over ``gh pr list --state merged``.
MERGE_EVIDENCE: MergeEvidence = GhprMergeEvidence()

__all__ = [
    "BEAD_STATE_LOOKUP",
    "CLAIM_RECORDS",
    "MERGE_EVIDENCE",
    "ArgvBeadStateLookup",
    "BeadsSessionBeadStateLookup",
    "ClaimAuthorityRecords",
    "GhprMergeEvidence",
    "RoutedBeadStateLookup",
]
