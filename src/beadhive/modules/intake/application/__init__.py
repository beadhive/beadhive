"""Intake's application layer: the service, its null stand-in, the thread service, the
supervised outbox sender and the module's own pager."""

from __future__ import annotations

from .discovery import ISSUES_CHECKPOINT, IssueDiscoverer, checkpoint_key
from .importer import IssueImporter, revision_token, snapshot_of
from .labels import LabelProjection
from .linker import LinkService
from .mentions import TrackerProjectIssues
from .milestones import MilestoneService
from .pacing import WritePacer, check_pace
from .paging import collect_snapshot, iterate_pages
from .proposals import ProposalService, TriageBinding
from .review import ReviewService
from .sender import OutboxService, StoreOutboxView
from .service import IntakeService, NullIntake, bind_intake
from .threads import ThreadService

__all__ = [
    "ISSUES_CHECKPOINT",
    "IssueDiscoverer",
    "IntakeService",
    "IssueImporter",
    "LabelProjection",
    "LinkService",
    "MilestoneService",
    "NullIntake",
    "OutboxService",
    "ProposalService",
    "ReviewService",
    "StoreOutboxView",
    "ThreadService",
    "TrackerProjectIssues",
    "TriageBinding",
    "WritePacer",
    "bind_intake",
    "check_pace",
    "checkpoint_key",
    "collect_snapshot",
    "iterate_pages",
    "revision_token",
    "snapshot_of",
]
