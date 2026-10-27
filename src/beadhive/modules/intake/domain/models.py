"""Intake-owned value records. Standard library only.

The domain holds intake's own records, never the tracker port's types (the checker forbids
``modules.intake.domain -> modules.tracker``): translation to and from ``TrackerIssue`` and
``IssueKey`` belongs to the application layer's mapping
(external-intake-module-adr.md section 9, resolution X2). Field names are tracker-neutral.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class IssueStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class Disposition(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    PROMOTE = "promote"
    REROUTE = "reroute"


class ImportKind(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


class LinkKind(StrEnum):
    LINKED = "linked"
    ALREADY_LINKED = "already_linked"
    RELINKED = "relinked"
    UNLINKED = "unlinked"


# -- store scope and views ------------------------------------------------------------------


@dataclass(frozen=True)
class HiveTriplet:
    """The ``provider/org/repo`` identity every report bead of one hive carries as labels."""

    provider: str
    org: str
    repo: str

    def labels(self) -> tuple[str, ...]:
        return (f"provider:{self.provider}", f"org:{self.org}", f"repo:{self.repo}")


@dataclass(frozen=True)
class StoreScope:
    """What a :class:`ReportStore` is bound to: one operational store of one hive."""

    operational_prefix: str
    hive: HiveTriplet
    public_prefixes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class IssueBinding:
    """The external issue a report is bound to. ``url`` is the key; the rest is opaque."""

    report: str
    tracker: str
    project: str
    url: str
    identifier: str


@dataclass(frozen=True)
class SourceSnapshot:
    """The last observed state of the bound issue (stored as ``intake.source``)."""

    title: str
    body: str
    state: IssueStatus
    revision: str
    closed_reason: str = ""
    labels: tuple[str, ...] = ()
    author_id: str = ""
    author_kind: str = "unknown"
    source_missing: bool = False


@dataclass(frozen=True)
class BeadView:
    """A report bead as the store reads it: just the fields intake decides on."""

    id: str
    title: str
    issue_type: str
    status: str = "open"
    priority: str = ""
    labels: tuple[str, ...] = ()
    source_system: str = ""
    external_ref: str = ""
    assignee: str = ""
    binding: IssueBinding | None = None
    snapshot: SourceSnapshot | None = None
    #: The store's own row for this bead, passed through untouched for a facade that must keep
    #: emitting the store's native shape (``bh work intake --json``). Never compared or decided on.
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class DuplicatePair:
    """Two beads the store flags as likely duplicates."""

    first: str
    second: str
    similarity: float
    #: The store's own pair record, passed through untouched (see :attr:`BeadView.raw`).
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)


# -- requests and outcomes -------------------------------------------------------------------


@dataclass(frozen=True)
class ReportDraft:
    """A report to file. ``origin`` is the ``origin:<channel>`` label-cache constant.

    ``binding`` and ``snapshot`` are set by import only; a born-native report (``bh report``,
    ``bh escalate``) carries neither."""

    title: str
    issue_type: str
    origin: str
    actor: str
    description: str = ""
    binding: IssueBinding | None = None
    snapshot: SourceSnapshot | None = None


@dataclass(frozen=True)
class ImportRequest:
    reference: str
    actor: str
    dry_run: bool = False


@dataclass(frozen=True)
class ImportOutcome:
    kind: ImportKind
    binding: IssueBinding
    dry_run: bool = False


@dataclass(frozen=True)
class LinkRequest:
    report: str
    work: str
    actor: str
    replace: bool = False


@dataclass(frozen=True)
class LinkOutcome:
    kind: LinkKind
    report: str
    work: str = ""


@dataclass(frozen=True)
class QueueQuery:
    """``channel`` narrows to one resolved ``origin`` channel; empty means every channel."""

    channel: str = ""
    include_duplicates: bool = True
    threshold: float = 0.5


@dataclass(frozen=True)
class QueueItem:
    id: str
    title: str
    issue_type: str
    channel: str
    labels: tuple[str, ...] = ()
    binding: IssueBinding | None = None
    #: The store's own row, passed through untouched (see :attr:`BeadView.raw`).
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class QueueView:
    items: tuple[QueueItem, ...] = ()
    duplicates: tuple[DuplicatePair, ...] = ()


@dataclass(frozen=True)
class DispositionRequest:
    """A triage disposition. ``seat`` is meaningful for ``reroute`` only: with a seat the report
    is bounced to that superintendent and stays untriaged; without one it is closed as rerouted
    (the root has already re-filed it in the target hive)."""

    report: str
    disposition: Disposition
    actor: str
    issue_type: str = ""
    priority: str = ""
    reason: str = ""
    seat: str = ""


@dataclass(frozen=True)
class DispositionOutcome:
    report: str
    disposition: Disposition
    state: str = ""  # the terminal intake value written; empty when the report stays queued
    issue_type: str = ""
    priority: str = ""
    seat: str = ""


@dataclass(frozen=True)
class DiscoveryRequest:
    actor: str
    full_reconcile: bool = False


@dataclass(frozen=True)
class DiscoveryOutcome:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    source_missing: int = 0
    checkpoint_advanced: bool = False
    reports: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Checkpoint:
    """An opaque, local, non-secret resume token for one binding."""

    token: str


__all__ = [
    "BeadView",
    "Checkpoint",
    "DiscoveryOutcome",
    "DiscoveryRequest",
    "Disposition",
    "DispositionOutcome",
    "DispositionRequest",
    "DuplicatePair",
    "HiveTriplet",
    "ImportKind",
    "ImportOutcome",
    "ImportRequest",
    "IssueBinding",
    "IssueStatus",
    "LinkKind",
    "LinkOutcome",
    "LinkRequest",
    "QueueItem",
    "QueueQuery",
    "QueueView",
    "ReportDraft",
    "SourceSnapshot",
    "StoreScope",
]
