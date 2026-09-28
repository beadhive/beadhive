"""Inbound ports the worktree safety classifier's policy depends on for bead-state, claim, and
merge-evidence facts (bh-qdezo.5).

Isolating these three narrow reads is the whole point: the package's classification policy
(``beadhive_worktrees.policy``) never imports ``bd``, ``claim_authority``, ``ghpr``, or
``subprocess`` — it depends only on these ``Protocol`` shapes. Root supplies the one argv-era
adapter per port today (``bd.json``/``bd.show`` reads, ``claim_authority`` record paths,
``ghpr.merged_pr_for``); a ``BeadsSession``-backed adapter is a later, additive swap
(bh-sy36q.6) that touches only the adapter, never the policy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class BeadStateLookup(Protocol):
    """Read access to a hive's bead store, scoped to exactly what the classifier needs.

    Mirrors the three reads the argv-era classifier made directly against ``bd``: a bounded
    listing used only to test whether the store answers AT ALL (``probe``), a per-bead record
    lookup (``show``) used both to resolve status/close_reason and to confirm a disposition's
    promised dependency edge, and an unbounded listing (``all_issues``) used to resolve
    batch-label membership evidence.
    """

    def probe(self, main: Path) -> list[Any] | None:
        """A bounded issue listing at ``main``, or ``None`` if the store could not be read at
        all (bd absent, a schema-fork guard refusing to open the database, or a store engine
        that is down)."""
        ...

    def show(self, bead_id: str, main: Path) -> dict[str, Any] | None:
        """One bead's fields (``status``, ``close_reason``, ``dependencies``, ...) at ``main``,
        or ``None`` if ``bead_id`` does not resolve."""
        ...

    def all_issues(self, main: Path) -> list[Any] | None:
        """Every issue in the hive at ``main`` (unbounded, including infra), for batch-label
        evidence resolution. ``None`` if the store could not be read."""
        ...


class ClaimRecords(Protocol):
    """Where a worktree's private claim-authority record lives, and how to retire it.

    Resolved before a worktree is removed: git deletes the linked-worktree's own admin metadata
    as part of removal, so the record path has to be captured first and deleted only once the
    removal itself succeeds.
    """

    def record_path(self, target: Any) -> Path | None:
        """The claim record path for the worktree at ``target``, or ``None`` if it cannot be
        resolved (``target`` accepts anything path-like: ``str`` or ``Path``)."""
        ...

    def remove_record_path(self, path: Path | None) -> None:
        """Delete the claim record at ``path``; a no-op if it is ``None`` or does not exist."""
        ...


class MergeEvidence(Protocol):
    """External, network-backed proof that a branch landed without patch-id equivalence.

    The squash-proof last resort in the ``is_landed`` cascade: a PR-governed land squash-merged
    on GitHub leaves neither a bh close_reason nor patch-id-matching commits, so the only
    remaining signal is whether a MERGED pull request has this branch as its head.
    """

    def merged_pr(self, entry: Any, branch: str) -> Any | None:
        """The MERGED pull request whose head is ``branch``, or ``None`` (no PR, a non-GitHub
        hive, or the ``gh`` CLI unavailable — always best-effort, never raises)."""
        ...


__all__ = ["BeadStateLookup", "ClaimRecords", "MergeEvidence"]
