"""Intake's application layer: the service, its null stand-in, the thread service and the
module's own pager."""

from __future__ import annotations

from .paging import collect_snapshot, iterate_pages
from .service import IntakeService, NullIntake, bind_intake
from .threads import ThreadService

__all__ = [
    "IntakeService",
    "NullIntake",
    "ThreadService",
    "bind_intake",
    "collect_snapshot",
    "iterate_pages",
]
