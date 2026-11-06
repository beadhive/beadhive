"""Concrete adapters of the intake module's outbound ports (``beadhive.modules.intake``).

:class:`BdReportStore` implements :class:`~beadhive.modules.intake.contracts.ReportStore` over an
injected ``beadhive_bd_cli`` route set. It imports the module's contracts and nothing else from
Beadhive; the composition root (``beadhive.intake_services``) supplies the routes, the store scope
and the failure-line parser. :class:`FileCheckpointStore` implements the module's
``CheckpointStore`` over atomic files in the hive's private state, and :class:`FileOutboxIndex`
the module's ``OutboxIndex`` (outbox id to report) the same way. :class:`ScrubSanitizer`
implements the module's ``Sanitizer`` over the shared ``beadhive_plugins.credentials.scrub``
until ``bh-o2mjz`` lands. :class:`GitReleaseVerifier` implements the ``ReleaseVerifier`` port
over local git, read-only, and :class:`GitRepoFacts` the ``RepoFacts`` port the proposal checks
read (ancestry, the computed fix version, changed paths) the same way. :class:`ApiReportStore`
serves the same port from the supervised Beads service (an injected :class:`ReportApi`), keeping
the ``bd`` routes only for what the service does not do (gates, ``set-state``, ``find-duplicates``).
"""

from .api_store import ApiReportStore, ReportApi, ReportApiError
from .bead_store import BdReportStore, ReportRoutes
from .checkpoints import FileCheckpointStore
from .documents import SOURCE_KEY, THREAD_KEY
from .outbox_index import FileOutboxIndex
from .release_verifier import GitReleaseVerifier
from .repo_facts import GitRepoFacts
from .sanitizer import ScrubSanitizer

__all__ = [
    "SOURCE_KEY",
    "THREAD_KEY",
    "ApiReportStore",
    "BdReportStore",
    "FileCheckpointStore",
    "FileOutboxIndex",
    "GitReleaseVerifier",
    "GitRepoFacts",
    "ReportApi",
    "ReportApiError",
    "ReportRoutes",
    "ScrubSanitizer",
]
