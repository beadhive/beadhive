"""Root-supplied adapters over bd argv, claim_authority record paths, and gh PR lookups.

The concrete ``BeadStateLookup`` / ``ClaimRecords`` / ``MergeEvidence`` ports
(``beadhive_worktrees.contracts``) the worktree safety classifier's policy depends on
(bh-qdezo.5). Composition, never policy: each method is a thin, behavior-preserving
pass-through to the pre-existing argv-era call, so swapping any one of these for a
``BeadsSession``-backed adapter later (bh-sy36q.6) never touches classification logic.

Every method resolves its collaborator (``bd``, ``claim_authority``, ``ghpr``) at call time
rather than caching a bound reference, so the existing per-module patch seams
(``worktree_inventory.bd``, ``worktree.bd``, ...) keep intercepting these calls unchanged —
they are all the same shared module object.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import bd, ghpr


class ArgvBeadStateLookup:
    """``BeadStateLookup`` over ``bd.json`` / ``bd.show`` subprocess reads."""

    def probe(self, main: Path) -> list[Any] | None:
        return bd.json(["list"], str(main))

    def show(self, bead_id: str, main: Path) -> dict[str, Any] | None:
        return bd.show(bead_id, str(main))

    def all_issues(self, main: Path) -> list[Any] | None:
        return bd.json(["list", "--all", "--include-infra", "--limit", "0"], str(main))


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


__all__ = ["ArgvBeadStateLookup", "ClaimAuthorityRecords", "GhprMergeEvidence"]
