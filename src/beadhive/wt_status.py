"""ws.wt_status — pure worktree status classifier.

Moved into :mod:`beadhive_worktrees.policy.classification` (bh-qdezo.5): the classification
policy depends only on the package's ``BeadStateLookup`` / ``ClaimRecords`` / ``MergeEvidence``
ports (see :mod:`beadhive_worktrees.contracts`), never on ``bd``, ``claim_authority``, ``ghpr``,
or ``subprocess`` directly. This module is a compatibility facade re-exporting the identical
names so every existing import and patch seam (``beadhive.wt_status.classify``, etc.) keeps
working unchanged (``docs/MODULES.md`` principle 8).

Root composition (``worktree_inventory.py``, ``worktree_cleanup.py``, ``worktree_git.py``)
supplies the argv-era adapters over the current ``bd.json``/``bd.show`` reads,
``claim_authority`` record paths, and ``ghpr.merged_pr_for`` — see
:mod:`beadhive.worktree_state_adapters`.
"""

from __future__ import annotations

from beadhive_worktrees.policy.classification import (
    BatchEvidence,
    WtClassification,
    WtDisposition,
    WtStatus,
    classify,
    format_disposition,
    parse_disposition,
    untrustworthy,
)

__all__ = [
    "BatchEvidence",
    "WtClassification",
    "WtDisposition",
    "WtStatus",
    "classify",
    "format_disposition",
    "parse_disposition",
    "untrustworthy",
]
