"""Claim-record and batch-checkout capabilities the lifecycle seam supplies to the core.

The assign / claim / release / provision policy moved to :mod:`beadhive_core.lifecycle`
(composed by :mod:`beadhive.work_lifecycle`, bh-sy36q.1). What remains here is shell-owned
operational capability: the host fencing token and the claim-authority record a claim or resume
writes, and the shared batch checkout per-bead verbs refuse to act on. Mutable collaborators are
supplied explicitly by the stable :mod:`beadhive.work` facade on each call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ClaimResult:
    """Structured result of the single-bead claim lifecycle.

    The CLI deliberately renders this result separately so composite callers can consume the
    complete provisioning record without scraping human progress output.
    """

    entry: dict
    main: Path
    bead: dict
    actor: str
    disposition: str
    worktree: Path
    identity: dict
    branch: str = ""


def impl__claim_fence(api, cfg, hive):
    try:
        this_host = api.host.host_id()
    except FileNotFoundError:
        this_host = ""
    return (this_host, api.guard.live_epoch(hive, cfg=cfg))


def impl__issue_claim(api, cfg, entry, bead, actor, target, hive):
    authority = api.claim_authority.get_authority(api.config.claim_authority(cfg, entry))
    this_host, epoch = api._claim_fence(cfg, hive)
    from .frame_eligibility import last_admission

    # The claim-time reread's admitted session/evidence stamps (bh-owqdg), when the frame's data
    # switched it onto session rows; passed only then, so older authorities keep working.
    admission = last_admission()
    extra = {"admission": admission} if admission else {}
    authority.issue(bead, actor, target, host_id=this_host, epoch=epoch, **extra)


def impl__batch_worktree(api, cfg, hive, bead, main):
    grp = api.work_group.batch_label(api.bd.show(bead, main))
    if not grp:
        return ("", None)
    target = api.worktree.locate(cfg, hive, branch=f"{api.work_group.BATCH_PREFIX}{grp}")[2]
    return (grp, target if target.exists() else None)
