"""Concrete adapters of the intake module's outbound ports (``beadhive.modules.intake``).

:class:`BdReportStore` implements :class:`~beadhive.modules.intake.contracts.ReportStore` over an
injected ``beadhive_bd_cli`` route set. It imports the module's contracts and nothing else from
Beadhive; the composition root (``beadhive.intake_services``) supplies the routes, the store scope
and the failure-line parser. :class:`FileCheckpointStore` implements the module's
``CheckpointStore`` over atomic files in the hive's private state. :class:`ScrubSanitizer`
implements the module's ``Sanitizer`` over the shared ``beadhive_plugins.credentials.scrub``
until ``bh-o2mjz`` lands.
"""

from .bead_store import BdReportStore, ReportRoutes
from .checkpoints import FileCheckpointStore
from .documents import SOURCE_KEY, THREAD_KEY
from .sanitizer import ScrubSanitizer

__all__ = [
    "SOURCE_KEY",
    "THREAD_KEY",
    "BdReportStore",
    "FileCheckpointStore",
    "ReportRoutes",
    "ScrubSanitizer",
]
