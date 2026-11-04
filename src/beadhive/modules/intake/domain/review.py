"""Review packets and triage-quality metrics (bh-hrx2r): the records of ``review_packets``.

The review pass hands an analyst or verifier everything the 2026-11-04 single pass lacked: each
report's body AND its full thread (the reporter's later comments split, narrowed or reopened five
of the reports it got wrong), its stored proposal with every review so far, its duplicate cluster,
and a global title/disposition index of every pending report, so a family is visible even across
batches. :class:`ReviewMetrics` makes a regression in triage quality visible: the correction rate
of each review pass and the error rate of reporter-visible outcomes, both computed from the
reviews stored on the proposals, never from a clock.

Pure: records and arithmetic only, no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .clusters import DEFAULT_BATCH_SIZE, Cluster, ClusterLink, ClusterPolicy
from .models import IssueBinding
from .proposal import Proposal, ProposalDisposition, ReviewVerdict
from .thread import ThreadView

#: The host switch that turns comment ingestion on (``IntakeService(threads=...)``).
COMMENTS_ENABLED_KEY = "intake.external.comments.enabled"
#: The tracker plugin switch that lets the adapter read comments (``comments.read``).
COMMENTS_READ_KEY = "plugins.tracker-github.read.comments.enabled"

#: The dispositions whose outcome a reporter is told: a fix claim, a reject reason, a question,
#: a duplicate notice. A wrong one tells a real person something false.
REPORTER_VISIBLE_DISPOSITIONS = frozenset(
    {
        ProposalDisposition.FIXED,
        ProposalDisposition.PARTIALLY_FIXED,
        ProposalDisposition.REJECT,
        ProposalDisposition.NEEDS_INFO,
        ProposalDisposition.DUPLICATE_OF,
    }
)


@dataclass(frozen=True)
class ReviewPolicy:
    """What the composition root says about this host (the module reads no configuration).

    ``comments_enabled`` is :data:`COMMENTS_ENABLED_KEY`; ``comments_read`` is whether the bound
    tracker's ``comments.read`` operation is usable (:data:`COMMENTS_READ_KEY` on GitHub, whose
    name ``comments_read_key`` carries for the refusal). Review refuses unless both are on, since a
    packet without the full thread is the error this exists to prevent.
    ``duplicate_threshold`` is what the store's duplicate pairs are read at."""

    comments_enabled: bool = False
    comments_read: bool = False
    comments_read_key: str = COMMENTS_READ_KEY
    duplicate_threshold: float = 0.5
    clusters: ClusterPolicy = ClusterPolicy()


@dataclass(frozen=True)
class ReviewRequest:
    """Build review packets for every pending report. ``actor`` attributes the thread ingest
    (the only write review makes); ``batch_size`` caps the reports per batch."""

    actor: str
    batch_size: int = DEFAULT_BATCH_SIZE


@dataclass(frozen=True)
class IndexEntry:
    """One pending report in the global index: enough to spot a family across batches.

    ``reference`` is the bound issue's identifier (empty for a born-native report);
    ``disposition``, ``status`` and ``share_with`` come from its proposal (empty: none yet)."""

    report: str
    title: str
    reference: str = ""
    disposition: str = ""
    status: str = ""
    share_with: str = ""
    cluster: str = ""
    batch: int = 0


@dataclass(frozen=True)
class ReviewPacket:
    """Everything one report's review reads.

    ``thread`` is the stored thread after ingest (``None``: a born-native report, which has no
    tracker thread); ``thread_complete`` is False when ingest could not read the whole thread
    (``thread_detail`` says why), so a reviewer knows the thread is partial. ``proposal`` carries
    the review history (``proposal.reviews``). ``cluster`` / ``cluster_members`` / ``links`` are
    the report's duplicate cluster; ``index`` is the global index of every pending report."""

    report: str
    title: str
    body: str
    labels: tuple[str, ...]
    binding: IssueBinding | None
    thread: ThreadView | None
    thread_complete: bool
    thread_detail: str
    proposal: Proposal | None
    cluster: str
    cluster_members: tuple[str, ...]
    links: tuple[ClusterLink, ...]
    index: tuple[IndexEntry, ...]


@dataclass(frozen=True)
class ReviewBatch:
    """One batch of whole clusters, numbered from 1, with a packet per member in order."""

    number: int
    clusters: tuple[Cluster, ...]
    packets: tuple[ReviewPacket, ...]

    @property
    def reports(self) -> tuple[str, ...]:
        return tuple(p.report for p in self.packets)


@dataclass(frozen=True)
class PassMetrics:
    """One review pass (``pass_no``) across every proposal reviewed that many times."""

    pass_no: int
    reviewed: int = 0
    confirmed: int = 0
    corrected: int = 0
    unsure: int = 0

    @property
    def correction_rate(self) -> float:
        return self.corrected / self.reviewed if self.reviewed else 0.0


@dataclass(frozen=True)
class ReviewMetrics:
    """Triage quality from stored reviews. ``passes`` per review pass in order;
    ``reporter_visible`` counts the reviewed proposals whose disposition a reporter is told and
    ``reporter_visible_errors`` those of them a review corrected."""

    proposals: int = 0
    reviewed: int = 0
    passes: tuple[PassMetrics, ...] = ()
    reporter_visible: int = 0
    reporter_visible_errors: int = 0

    @property
    def reporter_visible_error_rate(self) -> float:
        if not self.reporter_visible:
            return 0.0
        return self.reporter_visible_errors / self.reporter_visible


@dataclass(frozen=True)
class ReviewRun:
    """What ``review_packets`` built: the batches, the global index (also on every packet), every
    cluster, the reports whose thread it ingested first (in order) and the metrics."""

    batches: tuple[ReviewBatch, ...] = ()
    index: tuple[IndexEntry, ...] = ()
    clusters: tuple[Cluster, ...] = ()
    ingested: tuple[str, ...] = ()
    metrics: ReviewMetrics = ReviewMetrics()

    @property
    def reports(self) -> tuple[str, ...]:
        return tuple(r for b in self.batches for r in b.reports)


def is_reporter_visible(proposal: Proposal) -> bool:
    """Whether the proposal's outcome reaches the reporter: a reporter-visible disposition, or
    any proposal with reporter text."""
    return proposal.disposition in REPORTER_VISIBLE_DISPOSITIONS or bool(proposal.reporter_text)


def review_metrics(proposals: Iterable[Proposal] | Mapping[str, Proposal]) -> ReviewMetrics:
    """The correction rate of every review pass and the reporter-visible error rate.

    A proposal counts toward pass ``n`` when it has an ``n``-th review; a reporter-visible
    proposal with any review counts toward the reporter-visible rate, and is an error when any of
    its reviews ``corrected`` it. Deterministic: only the stored reviews are read."""
    values = list(proposals.values() if isinstance(proposals, Mapping) else proposals)
    counts: dict[int, dict[ReviewVerdict, int]] = {}
    reviewed = visible = errors = 0
    for proposal in values:
        if not proposal.reviews:
            continue
        reviewed += 1
        for review in proposal.reviews:
            tally = counts.setdefault(review.pass_no, {})
            tally[review.verdict] = tally.get(review.verdict, 0) + 1
        if is_reporter_visible(proposal):
            visible += 1
            if any(r.verdict is ReviewVerdict.CORRECTED for r in proposal.reviews):
                errors += 1
    passes = tuple(
        PassMetrics(
            pass_no,
            reviewed=sum(tally.values()),
            confirmed=tally.get(ReviewVerdict.CONFIRMED, 0),
            corrected=tally.get(ReviewVerdict.CORRECTED, 0),
            unsure=tally.get(ReviewVerdict.UNSURE, 0),
        )
        for pass_no, tally in sorted(counts.items())
    )
    return ReviewMetrics(len(values), reviewed, passes, visible, errors)


__all__ = [
    "COMMENTS_ENABLED_KEY",
    "COMMENTS_READ_KEY",
    "REPORTER_VISIBLE_DISPOSITIONS",
    "IndexEntry",
    "PassMetrics",
    "ReviewBatch",
    "ReviewMetrics",
    "ReviewPacket",
    "ReviewPolicy",
    "ReviewRequest",
    "ReviewRun",
    "is_reporter_visible",
    "review_metrics",
]
