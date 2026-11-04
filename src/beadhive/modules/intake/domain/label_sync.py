"""The label projection's requests, outcomes and pure planning (``bh-uim9i``, epic ``bh-hrx2r``).

What :class:`~..application.labels.LabelProjection` and the sender's batch approval speak across
``IntakePort``, plus the pure rules they apply. Nothing here reads a store, a tracker or a clock.

**Sync** (:class:`LabelSyncRequest` to :class:`LabelSyncOutcome`): one :class:`LabelSyncItem` per
report, carrying the converging diff (owned labels only), the drift (owned labels the projection
does not produce, removed only with ``prune_unmapped``), the exclusive-group swaps, the labels it
would add that the repository does not define (refused: ``bh work labels define`` first) and the
``labels`` record it drafted or found.

**Define** (:class:`LabelDefineRequest` to :class:`LabelDefineOutcome`): :func:`plan_definitions`
diffs the catalog's definitions against the repository's (:class:`LiveLabel`) into creates,
updates and, with ``prune``, deletes of owned labels the catalog no longer produces. A prune
candidate an OPEN issue still carries is kept and reported, never deleted. The plan's digest
(:func:`definitions_digest`) is what the operator approves in the command.

**Batch** (:class:`LabelBatch`): the ordered ``(outbox_id, digest)`` list of every drafted
``labels`` operation, each digest rendered against the base it was DRAFTED on, so the list (and its
digest, :func:`batch_digest`) does not move when an issue's labels do. An item whose live owned
labels moved since its draft is :attr:`LabelBatchItem.stale`: approving the batch skips it (it
mints no grant) and reports it for re-draft by the next sync.

:func:`progress_facts` decides what counts as an operator notice for the derived ``progress``
label: the newest progress-milestone record of the report (``started``, ``blocked``,
``deprioritised``) that was not retired (superseded or discarded unsent).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .labels import LabelCatalog, LabelProblem, LabelSpec, ProgressFacts, find_leaks
from .leaks import LeakFinding, LeakPolicy, Scanner
from .outbox import OutboxDocument, OutboxEvent, RecordStatus, _hex
from .sending import OutboxResult

#: The progress-milestone events whose newest live record is the report's operator notice.
NOTICE_EVENTS = frozenset({OutboxEvent.STARTED, OutboxEvent.BLOCKED, OutboxEvent.DEPRIORITISED})
#: The verb that defines labels, named by every refusal of an undefined one.
DEFINE_VERB = "bh work labels define"


def progress_facts(doc: OutboxDocument | None) -> ProgressFacts:
    """The operator notices that exist for a report: its newest ``started`` / ``blocked`` /
    ``deprioritised`` record that is not retired. A later notice replaces an earlier one, and a
    notice discarded (or superseded) before it was sent no longer counts."""
    if doc is None:
        return ProgressFacts()
    live = [
        r
        for r in doc.records.values()
        if r.event in NOTICE_EVENTS and r.status is not RecordStatus.RETIRED
    ]
    if not live:
        return ProgressFacts()
    newest = max(live, key=lambda r: r.created_seq)
    return ProgressFacts(
        blocked_notice=newest.event is OutboxEvent.BLOCKED,
        deprioritised_notice=newest.event is OutboxEvent.DEPRIORITISED,
    )


def _sha256(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# -- sync ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelSyncRequest:
    """``bh work labels sync``. ``reports`` empty: every bound report, the open ones unless
    ``include_closed`` (which also reads closed issues); named reports are synced whatever their
    status. ``dry_run`` reports the diffs and writes nothing; ``prune_unmapped`` also removes the
    drift (owned labels the projection does not produce)."""

    actor: str
    reports: tuple[str, ...] = ()
    include_closed: bool = False
    dry_run: bool = False
    prune_unmapped: bool = False


@dataclass(frozen=True)
class LabelSyncItem:
    """One report's sync. ``add`` / ``remove`` converge its issue's owned labels; ``drift`` is
    reported (and in ``remove`` only with ``prune_unmapped``); ``swaps`` the exclusive-group
    swaps as ``(dimension, removed, added)``; ``undefined`` the labels it would add that the
    repository does not define (nothing is drafted then). ``drafted`` is a new (refreshed) labels
    record, ``pending`` the open one already carrying this diff, ``superseded`` the older open one
    the new draft retired, ``discarded`` an open one the converged issue no longer needs."""

    report: str
    url: str = ""
    add: frozenset[str] = frozenset()
    remove: frozenset[str] = frozenset()
    drift: frozenset[str] = frozenset()
    undefined: frozenset[str] = frozenset()
    swaps: tuple[tuple[str, str, str], ...] = ()
    drafted: str = ""
    pending: str = ""
    superseded: str = ""
    discarded: str = ""
    held: bool = False
    note: str = ""
    error: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.add or self.remove)


@dataclass(frozen=True)
class LabelSyncOutcome:
    items: tuple[LabelSyncItem, ...] = ()
    dry_run: bool = False

    @property
    def drafted(self) -> tuple[str, ...]:
        return tuple(i.drafted for i in self.items if i.drafted)

    @property
    def drift(self) -> Mapping[str, frozenset[str]]:
        return MappingProxyType({i.report: i.drift for i in self.items if i.drift})

    @property
    def errors(self) -> tuple[LabelSyncItem, ...]:
        return tuple(i for i in self.items if i.error)


# -- define ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveLabel:
    """A label definition as the repository holds it."""

    name: str
    color: str
    description: str


@dataclass(frozen=True)
class KeptLabel:
    """A prune candidate that is NOT deleted: an open issue still carries it."""

    name: str
    reason: str
    carried_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class LabelDefinePlan:
    """What ``define`` would do to the repository's label definitions, and its digest.
    ``findings`` (leaks) and ``problems`` (a name the tracker refuses) of the definitions it would
    write block the approval."""

    target: str
    create: tuple[LabelSpec, ...] = ()
    update: tuple[LabelSpec, ...] = ()
    delete: tuple[str, ...] = ()
    kept: tuple[KeptLabel, ...] = ()
    problems: tuple[LabelProblem, ...] = ()
    findings: Mapping[str, tuple[LeakFinding, ...]] = field(default_factory=dict)
    digest: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.create or self.update or self.delete)

    @property
    def short_digest(self) -> str:
        return _hex(self.digest)[:12]

    @property
    def blocked(self) -> bool:
        return bool(self.findings or self.problems)


@dataclass(frozen=True)
class LabelDefineRequest:
    """``bh work labels define``: without ``approve`` a dry run (the plan and its digest); with
    the digest an operator reviewed, apply it. ``prune`` adds the deletes."""

    actor: str
    approve: str = ""
    prune: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class LabelDefineOutcome:
    """The plan, and whether it was applied: with which single-use grants (one per tracker
    write), and the local audit note recorded."""

    plan: LabelDefinePlan
    applied: bool = False
    grant_ids: tuple[str, ...] = ()
    audit: str = ""
    detail: str = ""


def definitions_digest(
    target: str,
    create: Iterable[LabelSpec],
    update: Iterable[LabelSpec],
    delete: Iterable[str],
) -> str:
    """sha256 over the canonical plan: the target and every definition write it would make."""
    return _sha256(
        {
            "create": [[s.name, s.color, s.description] for s in create],
            "delete": list(delete),
            "target": target,
            "update": [[s.name, s.color, s.description] for s in update],
        }
    )


def plan_definitions(
    catalog: LabelCatalog,
    live: Iterable[LiveLabel],
    *,
    target: str,
    policy: LeakPolicy,
    sanitizer: Scanner | None,
    prune: bool = False,
    carried: Mapping[str, tuple[str, ...]] | None = None,
) -> LabelDefinePlan:
    """The catalog's definitions against the repository's: create what is missing, update an
    owned label whose color or description differs, and (``prune``) delete the owned labels the
    catalog does not produce, except those an open issue carries (``carried``: label name to
    the open issues carrying it), which are kept and reported. Labels outside the prefix are never
    in the plan. Converged: nothing to create, update or delete."""
    have = {d.name: d for d in live}
    create: list[LabelSpec] = []
    update: list[LabelSpec] = []
    for spec in catalog.specs:
        current = have.get(spec.name)
        if current is None:
            create.append(spec)
        elif (current.color.lower(), current.description) != (spec.color, spec.description):
            update.append(spec)
    delete: list[str] = []
    kept: list[KeptLabel] = []
    if prune:
        carriers = carried or {}
        for unmapped in catalog.unmapped(have):
            on = tuple(carriers.get(unmapped.name, ()))
            if on:
                kept.append(KeptLabel(unmapped.name, unmapped.reason.value, on))
            else:
                delete.append(unmapped.name)
    writes = {s.name for s in (*create, *update)}
    findings = {
        name: found
        for name, found in find_leaks((*create, *update), policy, sanitizer=sanitizer).items()
    }
    problems = tuple(p for p in catalog.problems() if p.name in writes)
    return LabelDefinePlan(
        target,
        tuple(create),
        tuple(update),
        tuple(delete),
        tuple(kept),
        problems,
        MappingProxyType(findings),
        definitions_digest(target, create, update, delete),
    )


# -- the batch -------------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelBatchItem:
    """One drafted ``labels`` operation in a batch: its digest as drafted, the change it makes,
    and whether the issue's live owned labels moved since the draft (``stale``)."""

    report: str
    outbox_id: str
    digest: str
    url: str
    add: frozenset[str] = frozenset()
    remove: frozenset[str] = frozenset()
    stale: bool = False
    note: str = ""

    @property
    def short_digest(self) -> str:
        return _hex(self.digest)[:12]


@dataclass(frozen=True)
class LabelBatch:
    """The rendered batch: every drafted labels operation, ordered, and the digest of the
    ``(outbox_id, digest)`` list the operator approves."""

    items: tuple[LabelBatchItem, ...] = ()
    digest: str = ""

    @property
    def short_digest(self) -> str:
        return _hex(self.digest)[:12]

    @property
    def stale(self) -> tuple[LabelBatchItem, ...]:
        return tuple(i for i in self.items if i.stale)


@dataclass(frozen=True)
class LabelBatchApprove:
    """Approve a rendered batch by its digest (the full value or at least its 12-hex prefix)."""

    digest: str
    actor: str


@dataclass(frozen=True)
class LabelBatchOutcome:
    """``approved`` has one result (with its single-use grant) per approved item; ``stale`` the
    items skipped because the issue's labels moved (re-run sync to re-draft them); ``failed`` the
    items an approval refused, with why."""

    batch: LabelBatch
    approved: tuple[OutboxResult, ...] = ()
    stale: tuple[LabelBatchItem, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()

    @property
    def grant_ids(self) -> tuple[str, ...]:
        return tuple(r.grant_id for r in self.approved if r.grant_id)


def batch_digest(items: Iterable[LabelBatchItem]) -> str:
    """sha256 over the ordered ``(outbox_id, digest)`` list."""
    return _sha256([[i.outbox_id, i.digest] for i in items])


__all__ = [
    "DEFINE_VERB",
    "NOTICE_EVENTS",
    "KeptLabel",
    "LabelBatch",
    "LabelBatchApprove",
    "LabelBatchItem",
    "LabelBatchOutcome",
    "LabelDefineOutcome",
    "LabelDefinePlan",
    "LabelDefineRequest",
    "LabelSyncItem",
    "LabelSyncOutcome",
    "LabelSyncRequest",
    "LiveLabel",
    "batch_digest",
    "definitions_digest",
    "plan_definitions",
    "progress_facts",
]
