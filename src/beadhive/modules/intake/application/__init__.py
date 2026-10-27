"""Intake's application layer: the service, its null stand-in, the thread service, the
supervised outbox sender and the module's own pager."""

from __future__ import annotations

from .discovery import ISSUES_CHECKPOINT, IssueDiscoverer, checkpoint_key
from .importer import IssueImporter, revision_token, snapshot_of
from .linker import LinkService
from .paging import collect_snapshot, iterate_pages
from .sender import OutboxService, StoreOutboxView
from .service import IntakeService, NullIntake, bind_intake
from .threads import ThreadService

__all__ = [
    "ISSUES_CHECKPOINT",
    "IssueDiscoverer",
    "IntakeService",
    "IssueImporter",
    "LinkService",
    "NullIntake",
    "OutboxService",
    "StoreOutboxView",
    "ThreadService",
    "bind_intake",
    "checkpoint_key",
    "collect_snapshot",
    "iterate_pages",
    "revision_token",
    "snapshot_of",
]
