"""Intake's application layer: the service, its null stand-in and the module's own pager."""

from __future__ import annotations

from .paging import collect_snapshot, iterate_pages
from .service import IntakeService, NullIntake, bind_intake

__all__ = ["IntakeService", "NullIntake", "bind_intake", "collect_snapshot", "iterate_pages"]
