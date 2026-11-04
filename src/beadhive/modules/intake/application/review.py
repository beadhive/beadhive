"""``ReviewService``: review packets over the pending reports (bh-hrx2r).

The pending set is every open report triage still owes a disposition and that has no APPLIED
proposal: each ``intake:untriaged`` report (a report its reporter woke is back there), and each
``intake:needs-info`` report whose thread now holds a reply (woken, or about to be). Review first
INGESTS every bound candidate's thread through the :class:`~.threads.ThreadService`, so every packet
carries the full, current thread; with comment ingestion off, or the tracker's ``comments.read``
unusable, it refuses before reading anything and names both switches. Then it clusters the pending
reports (:func:`..domain.clusters.cluster`), packs whole clusters into batches and builds one
packet per report with the global title/disposition index of every pending report.

The only write is the thread ingest (one ``intake.thread`` write per changed thread); nothing is
sent, labelled, dispositioned or proposed. The switches come in as a :class:`ReviewPolicy`: this
module reads no configuration. No clock: ordering is by issue number, then bead id.
"""

from __future__ import annotations

from ..contracts import ReportStore
from ..domain import (
    INTAKE_NEEDS_INFO,
    INTAKE_UNTRIAGED,
    BeadView,
    IntakeRefused,
    Proposal,
    ProposalStatus,
    RefusalReason,
    ThreadIssue,
    ThreadRequest,
    ThreadView,
    new_thread,
    version_key,
    wakes,
)
from ..domain.clusters import Cluster, ClusterItem, batches, cluster
from ..domain.review import (
    COMMENTS_ENABLED_KEY,
    IndexEntry,
    ReviewBatch,
    ReviewMetrics,
    ReviewPacket,
    ReviewPolicy,
    ReviewRequest,
    ReviewRun,
    review_metrics,
)
from .threads import ThreadService


def comments_required(policy: ReviewPolicy) -> IntakeRefused:
    """The refusal of a review that cannot read every thread: it names both switches."""
    return IntakeRefused(
        RefusalReason.INTAKE_DISABLED,
        "review needs every pending report's full thread, so comment ingestion must be on: set "
        f"{COMMENTS_ENABLED_KEY} and {policy.comments_read_key} to true, then run review again",
    )


def body_of(view: BeadView) -> str:
    """The report's body: the bound issue's last observed body, else the bead's description."""
    if view.snapshot is not None:
        return view.snapshot.body
    description = view.raw.get("description", "") if view.raw else ""
    return description if isinstance(description, str) else ""


def _order(view: BeadView) -> tuple:
    """Issue-number order for bound reports, then born-native reports by id."""
    if view.binding is not None:
        return (0, version_key(view.binding.identifier), view.id)
    return (1, version_key(view.id), view.id)


class ReviewService:
    """Builds review packets over one store. ``threads`` is the bound thread service (``None``:
    comment ingestion is off); ``policy`` is the host's switches and thresholds."""

    def __init__(
        self,
        store: ReportStore,
        threads: ThreadService | None,
        policy: ReviewPolicy | None = None,
    ) -> None:
        self._store = store
        self._threads = threads
        self._policy = policy or ReviewPolicy()

    # -- metrics (store reads only; no switch needed) -----------------------------------------

    def proposals(self) -> dict[str, Proposal]:
        """Every stored proposal, by report id (sorted)."""
        found = {
            view.id: self._store.read_proposal(view.id)
            for view in self._store.reports_with_proposal()
        }
        return {report: found[report] for report in sorted(found) if found[report] is not None}

    def metrics(self) -> ReviewMetrics:
        """The per-pass correction rate and reporter-visible error rate of every stored proposal."""
        return review_metrics(self.proposals())

    # -- packets ------------------------------------------------------------------------------

    def _require_threads(self) -> ThreadService:
        policy = self._policy
        if self._threads is None or not (policy.comments_enabled and policy.comments_read):
            raise comments_required(policy)
        return self._threads

    def packets(self, request: ReviewRequest) -> ReviewRun:
        """Ingest every candidate's thread, then build cluster-aware batches of packets."""
        if request.batch_size < 1:
            raise IntakeRefused(RefusalReason.INVALID_REQUEST, "the batch size must be at least 1")
        threads = self._require_threads()
        proposals = self.proposals()
        candidates: dict[str, BeadView] = {}
        for label in (INTAKE_UNTRIAGED, INTAKE_NEEDS_INFO):
            for view in self._store.list_open_labeled(label):
                applied = proposals.get(view.id)
                if applied is None or applied.status is not ProposalStatus.APPLIED:
                    candidates.setdefault(view.id, view)
        ordered = sorted(candidates.values(), key=_order)

        ingested: list[str] = []
        status: dict[str, tuple[bool, str]] = {}
        for view in ordered:
            if view.binding is None:
                status[view.id] = (True, "born-native: no tracker thread")
                continue
            try:
                outcome = threads.ingest_thread(ThreadRequest(view.id, request.actor))
            except IntakeRefused as exc:
                if exc.reason is not RefusalReason.OTHER_PROJECT:
                    raise
                status[view.id] = (False, exc.detail or exc.reason.value)
                continue
            ingested.append(view.id)
            status[view.id] = (outcome.complete, outcome.detail)

        views = {v.id: _thread(v, threads.thread_of(v.id)) for v in ordered}
        pending = [v for v in ordered if _pending(v, views[v.id])]

        items = [
            ClusterItem(
                v.id,
                v.title,
                body_of(v),
                proposals[v.id].evidence if v.id in proposals else (),
            )
            for v in pending
        ]
        pairs = self._store.duplicate_pairs(threshold=self._policy.duplicate_threshold)
        clusters = cluster(items, pairs, self._policy.clusters)
        packed = batches(clusters, request.batch_size)

        cluster_of: dict[str, Cluster] = {m: c for c in clusters for m in c.members}
        batch_of = {m: b.number for b in packed for m in b.members}
        index = tuple(
            _entry(v, proposals.get(v.id), cluster_of[v.id].key, batch_of[v.id]) for v in pending
        )
        by_id = {v.id: v for v in pending}

        def packet(report: str) -> ReviewPacket:
            view, group = by_id[report], cluster_of[report]
            complete, detail = status[report]
            return ReviewPacket(
                report=report,
                title=view.title,
                body=body_of(view),
                labels=view.labels,
                binding=view.binding,
                thread=views[report],
                thread_complete=complete,
                thread_detail=detail,
                proposal=proposals.get(report),
                cluster=group.key,
                cluster_members=group.members,
                links=tuple(k for k in group.links if report in (k.first, k.second)),
                index=index,
            )

        return ReviewRun(
            batches=tuple(
                ReviewBatch(b.number, b.clusters, tuple(packet(m) for m in b.members))
                for b in packed
            ),
            index=index,
            clusters=clusters,
            ingested=tuple(ingested),
            metrics=review_metrics(proposals),
        )


def _thread(view: BeadView, stored: ThreadView | None) -> ThreadView | None:
    """The stored thread; an empty one for a bound report with no comment yet (ingest wrote
    nothing); ``None`` only for a born-native report, which has no tracker thread."""
    if stored is not None or view.binding is None:
        return stored
    binding = view.binding
    empty = new_thread(
        binding.tracker, binding.project, ThreadIssue(binding.identifier, binding.url)
    )
    return ThreadView(view.id, empty)


def _pending(view: BeadView, thread: ThreadView | None) -> bool:
    """Untriaged (woken included), or waiting on the reporter with a reply in the thread."""
    if INTAKE_UNTRIAGED in view.labels:
        return True
    return thread is not None and wakes(thread.new_evidence)


def _entry(view: BeadView, proposal: Proposal | None, cluster_key: str, batch: int) -> IndexEntry:
    return IndexEntry(
        report=view.id,
        title=view.title,
        reference=view.binding.identifier if view.binding is not None else "",
        disposition=proposal.disposition.value if proposal is not None else "",
        status=proposal.status.value if proposal is not None else "",
        share_with=proposal.share_with if proposal is not None else "",
        cluster=cluster_key,
        batch=batch,
    )


__all__ = ["ReviewService", "body_of", "comments_required"]
