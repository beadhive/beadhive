"""``IssueDiscoverer``: opt-in, pull-only discovery of external issues (I6).

The operation behind ``IntakePort.discover``, called by the operator verb ``bh work discover
[--full]`` and by the opt-in host-daemon timer alike (external-intake-module-adr.md section 4,
``bh-mj7cl`` section 6). It runs the SAME upsert as import (:meth:`IssueImporter.upsert`) for each
issue the tracker lists, so a discovered report is exactly an imported one: held, untriaged,
``origin:github``, a re-pull of an unchanged revision a no-op. It holds a tracker reader only (no
writer, no ``WriteGrant``), never the adapter's internals and never ``bd github sync``.

* **Incremental.** Page ``list_issues(IssueQuery(since=<checkpoint>, include_closed=True))`` with
  intake's own pager. The checkpoint is the TRACKER's own high-water mark (a page's ``checkpoint``,
  never the local clock) and goes through :class:`CheckpointStore` only after that page and every
  earlier page of the pull are durably upserted. A crash mid-pull therefore resumes from the last
  durable page; ``since`` is inclusive, so the boundary issue repeats and is ``unchanged``.
* **Full** (``--full`` or the timer's every-Nth tick). The same pull WITHOUT ``since`` (open and
  closed issues, so a newly closed issue is refreshed whatever the checkpoint says), then the
  full id set (:func:`collect_snapshot`) to judge ``source_missing``. Only a COMPLETE snapshot may
  mark a bound report ``source_missing`` in its snapshot (or clear it again when the issue is
  back); no bead is ever deleted, closed or otherwise changed by discovery.
* **Lock.** One :class:`IntakeLock` hold per run, keyed on the binding, shared with import: a
  second discovery or an import in flight is ``INTAKE_BUSY`` (never waits, never interleaves).
* **Failure.** Any refusal or store failure on an issue stops the run at once: the checkpoint is
  never moved past an issue that was not ingested, so the next run re-pulls it (idempotent).
"""

from __future__ import annotations

import dataclasses

from beadhive.modules.tracker.contracts import Checkpoint as TrackerCheckpoint
from beadhive.modules.tracker.contracts import (
    IdQuery,
    IdSubject,
    IssueQuery,
    OperationNotEnabled,
    TrackerError,
    TrackerReader,
)

from ..contracts import CheckpointStore, IntakeLock, ReportStore
from ..domain import Checkpoint, DiscoveryOutcome, DiscoveryRequest, ImportKind, IssueBinding
from .importer import IssueImporter, _disabled_read, _tracker_error
from .paging import collect_snapshot, iterate_pages
from .threads import binding_key

#: The checkpoint namespace of the issue-discovery pull. The comment sweep (a later bead) has its
#: own key under the same binding, so the two never share a mark.
ISSUES_CHECKPOINT = "issues"


def checkpoint_key(binding: IssueBinding) -> str:
    """The :class:`CheckpointStore` key of ``binding``'s issue-discovery mark."""
    return f"{ISSUES_CHECKPOINT}:{binding_key(binding)}"


class IssueDiscoverer:
    """Implements ``IntakePort.discover`` over one store, one tracker reader and one importer."""

    def __init__(
        self,
        store: ReportStore,
        tracker: TrackerReader,
        importer: IssueImporter,
        lock: IntakeLock,
        checkpoints: CheckpointStore,
        *,
        page_size: int | None = None,
    ) -> None:
        self._store = store
        self._tracker = tracker
        self._importer = importer
        self._lock = lock
        self._checkpoints = checkpoints
        self._page_size = page_size

    def discover(self, request: DiscoveryRequest) -> DiscoveryOutcome:
        try:
            project = self._tracker.describe().project
            binding = IssueBinding("", project.tracker, project.locator, "", "")
            with self._lock.hold(binding_key(binding)):
                return self._run(request, binding)
        except OperationNotEnabled as exc:
            raise _disabled_read(exc) from exc
        except TrackerError as exc:
            raise _tracker_error(exc) from exc

    # -- the pull ------------------------------------------------------------------------------

    def _run(self, request: DiscoveryRequest, binding: IssueBinding) -> DiscoveryOutcome:
        key = checkpoint_key(binding)
        stored = None if request.full_reconcile else self._checkpoints.get(key)
        since = TrackerCheckpoint(stored.token) if stored is not None else None
        last = stored.token if stored is not None else None
        counts = {ImportKind.CREATED: 0, ImportKind.UPDATED: 0, ImportKind.UNCHANGED: 0}
        touched: list[str] = []
        advanced = False

        def fetch(cursor):
            return self._tracker.list_issues(
                IssueQuery(
                    since=since, cursor=cursor, include_closed=True, page_size=self._page_size
                )
            )

        for page in iterate_pages(fetch):
            for issue in page.items:
                outcome = self._importer.upsert(issue, request.actor)
                counts[outcome.kind] += 1
                if outcome.kind is not ImportKind.UNCHANGED:
                    touched.append(outcome.binding.report)
            # durable: every issue of this page and of every earlier page is upserted
            token = page.checkpoint.token if page.checkpoint is not None else None
            if token is not None and token != last:
                self._checkpoints.put(key, Checkpoint(token))
                last, advanced = token, True
        missing, reconciled = 0, False
        if request.full_reconcile:
            missing, cleared, reconciled = self._reconcile(binding, request.actor)
            counts[ImportKind.UPDATED] += cleared
        return DiscoveryOutcome(
            created=counts[ImportKind.CREATED],
            updated=counts[ImportKind.UPDATED],
            unchanged=counts[ImportKind.UNCHANGED],
            source_missing=missing,
            checkpoint_advanced=advanced,
            reports=tuple(touched),
            reconciled=reconciled,
        )

    # -- the full id reconcile -------------------------------------------------------------------

    def _reconcile(self, binding: IssueBinding, actor: str) -> tuple[int, int, bool]:
        """Judge ``source_missing`` against the project's full id set. Returns ``(newly marked,
        cleared, complete)``. An incomplete snapshot changes nothing: a deletion is never inferred
        from a listing that failed part-way."""
        snapshot = collect_snapshot(self._tracker, IdQuery(IdSubject.ISSUES))
        if not snapshot.complete:
            return 0, 0, False
        marked = cleared = 0
        for view in self._store.bound_reports():
            bound = view.binding
            if (
                bound is None
                or view.snapshot is None
                or (bound.tracker, bound.project) != (binding.tracker, binding.project)
            ):
                continue  # another project's report, or one an interrupted create left bare
            present = bound.url in snapshot.ids
            if present == (not view.snapshot.source_missing):
                continue
            flagged = dataclasses.replace(view.snapshot, source_missing=not present)
            self._store.record_snapshot(view.id, flagged, actor=actor)
            if present:
                cleared += 1
            else:
                marked += 1
        return marked, cleared, True


__all__ = ["ISSUES_CHECKPOINT", "IssueDiscoverer", "checkpoint_key"]
