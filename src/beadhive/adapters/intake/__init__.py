"""Concrete adapters of the intake module's outbound ports (``beadhive.modules.intake``).

:class:`BdReportStore` implements :class:`~beadhive.modules.intake.contracts.ReportStore` over an
injected ``beadhive_bd_cli`` route set. It imports the module's contracts and nothing else from
Beadhive; the composition root (``beadhive.intake_services``) supplies the routes, the store scope
and the failure-line parser.
"""

from .bead_store import BdReportStore, ReportRoutes
from .documents import SOURCE_KEY, THREAD_KEY

__all__ = ["SOURCE_KEY", "THREAD_KEY", "BdReportStore", "ReportRoutes"]
