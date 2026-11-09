"""``LabelProjection``: bead state onto the bound issues' OWNED labels (``bh-uim9i``, epic
``bh-hrx2r``), beside :class:`~.milestones.MilestoneService`.

The pure rules live in :mod:`..domain.labels` (the registry-derived catalog, the desired state, the
diff) and :mod:`..domain.label_sync` (the records and the definitions plan); this layer reads the
store and the tracker and drafts.

**sync** (:meth:`LabelProjection.sync`): for every selected bound report, the desired owned labels
from the report's own facts, its tracked work's (type, priority and component come from the work,
falling back to the report) and its operator notices (:func:`~..domain.label_sync.progress_facts`),
diffed against the issue's labels read FRESH: ONE issue listing per run (open issues, plus closed
ones with ``include_closed``), never one read per issue, so a re-run converges on what the issue
carries now, not on the import snapshot. A non-empty diff drafts at most ONE ``labels`` record per
issue under the binding's :class:`IntakeLock`: a re-run with the same diff finds it (O2), a new
diff refreshes it (O6 supersedes the older one), so records never stack. Labels outside the
prefix are never touched; owned labels the projection does not produce are drift, reported and
removed only with ``prune_unmapped``. A diff that would ADD a label the repository does not define
is refused for that issue (``bh work labels define`` first), from ONE ``labels.read``. Nothing is
sent: an operator approves the batch (:meth:`~.sender.OutboxService.approve_label_batch`) and
``send`` changes each issue once.

**The issue type and field values** (``bh-poqy0``): with ``values`` (the maps,
:class:`~..domain.issue_values.ValueSettings`) and the outbox's ``issue.type`` / ``issue.fields``
gate on, the same pass computes the type and the priority field value the bead projects (the
tracked work, falling back to the report) and diffs them against the issue's live ones from the
same listing; a difference is reported like drift and drafted as an ``issue.type`` /
``issue.fields`` operation on the SAME ``labels`` record, so one batch review approves them with
the labels. An unmapped value, or one the issue was not read for, is skipped with a note. Labels
are unchanged by this: a record may carry any of the three operations.

**refresh** (:meth:`LabelProjection.refresh`, ``bh-gjb5w``): the opt-in transition hook's draft
(``intake.external.labels.on_transition``). The same desired-state function and the same single
``labels`` record per issue, but TRACKER-FREE: it diffs against the labels the store holds (the
import snapshot, with the owned part replaced by the base of the open ``labels`` record when one
is waiting, so a refresh never regresses a fresher sync draft) and reads no issue listing and no
label definitions. The sender reads the live labels when it renders, so a stale base only
costs a no-op change. Same supersede group: a refresh finds (O2) or refreshes (O6) the report's
open record, never stacking one. The issue's type and field values are believed from the outbox
(the base of the open record's value operation, else the last one sent); unknown ones are drafted
unconditionally, and the next sync discards what the issue already holds.

**define** (:meth:`LabelProjection.define`): the catalog (closed sets up front, open sets as the
values appear on the projected beads) against the repository's definitions, rendered with a
digest. With the digest an operator reviewed, an operator seat re-renders it, mints ONE
``ProjectRef`` grant for the create/update write (and one for the prune's delete write, when there
is one) and applies it; prune deletes only unmapped owned labels no OPEN issue carries (ONE open
issue listing). A converged repository needs no grant. The application is noted in the injected
local :class:`~..contracts.AuditLog`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager, nullcontext

from beadhive.modules.tracker.contracts import (
    IssueQuery,
    LabelDefiner,
    LabelDefinition,
    LabelPruner,
    LabelReader,
    Operation,
    PageCursor,
    TrackerAdapter,
    TrackerError,
    TrackerIssue,
    TrackerReader,
    WriteGrant,
)

from ..contracts import AuditLog, BatchLock, IdSource, IntakeLock, ReportStore, Sanitizer
from ..domain import (
    DEFINE_VERB,
    BeadView,
    DraftRequest,
    IntakeRefused,
    IntakeStoreError,
    IssueBinding,
    LabelBase,
    LabelCatalog,
    LabelDefineOutcome,
    LabelDefinePlan,
    LabelDefineRequest,
    LabelIntent,
    LabelSettings,
    LabelSyncItem,
    LabelSyncOutcome,
    LabelSyncRequest,
    LeakPolicy,
    LiveLabel,
    OpKind,
    OpState,
    OutboxDocument,
    OutboxEvent,
    OutboxPolicy,
    ProgressFacts,
    RecordStatus,
    RefusalReason,
    RegistryDimension,
    RenderContext,
    ReportFacts,
    StorePosture,
    ThreadState,
    ValueChange,
    ValueIntent,
    ValueSettings,
    WorkFacts,
    desired_labels,
    desired_values,
    diff_labels,
    field_changes,
    labels_revision,
    plan_definitions,
    progress_facts,
    require_private_store,
    type_change,
)
from ..domain import outbox as machine
from ..domain.labels import COMPONENT_DIM, TAG_DIM
from ..domain.outbox import TYPE_VALUE
from .importer import revision_token
from .paging import iterate_pages
from .threads import held as held_binding
from .threads import store_session

#: The states of a label operation whose write may be in flight: no new draft until reconciled.
_IN_FLIGHT = frozenset({OpState.SENDING, OpState.UNKNOWN})
#: The states of an operation still waiting for its approval or its send.
_OPEN = frozenset({OpState.DRAFTED, OpState.HELD, OpState.APPROVED})


def label_base(issue: TrackerIssue) -> LabelBase:
    """An issue's labels as one fresh read saw them, with the tracker's revision."""
    return LabelBase(revision_token(issue.revision), frozenset(issue.labels))


def issue_index(tracker: TrackerReader, *, include_closed: bool) -> dict[str, TrackerIssue]:
    """ONE paged issue listing, by URL (the join key): the bulk read a run makes instead of one
    read per issue. Raises the port's ``TrackerError`` when the listing fails part-way, so no
    label state is inferred from an incomplete read."""
    index: dict[str, TrackerIssue] = {}

    def fetch(cursor: PageCursor | None):
        return tracker.list_issues(IssueQuery(cursor=cursor, include_closed=include_closed))

    for page in iterate_pages(fetch):
        for issue in page.items:
            index[issue.key.url] = issue
    return index


@dataclasses.dataclass(frozen=True)
class _Facts:
    report: ReportFacts
    work: WorkFacts | None
    progress: ProgressFacts
    #: The report's outbox as the facts read it (bh-25fvb): the converged check reuses it rather
    #: than reading it again; a discard still re-reads it under the lock.
    outbox: OutboxDocument | None = None


class LabelProjection:
    """The label projection over one :class:`ReportStore` and one tracker.

    ``registry`` is the dimension registry (``dimensions.*``) and ``settings`` the projection
    settings, both translated from config by the composition root. ``outbox`` is the hive's outbox
    policy: drafting needs it (and its ``labels_enabled``) on, and its agent-seat prefixes decide
    who is an operator. ``leak_policy`` must own the prefix (``owned_label_prefixes``);
    ``sanitizer=None`` holds every drafted record and blocks every definition (fail closed).
    ``audit`` receives the local note of an applied ``define``. ``values`` are the issue type
    and field maps; without them (or with the outbox's ``issue.type`` / ``issue.fields`` gate
    off) no value is projected."""

    def __init__(
        self,
        store: ReportStore,
        tracker: TrackerAdapter,
        lock: IntakeLock,
        *,
        ids: IdSource,
        registry: Mapping[str, RegistryDimension] | Iterable[RegistryDimension],
        settings: LabelSettings,
        leak_policy: LeakPolicy,
        sanitizer: Sanitizer | None,
        posture: StorePosture,
        outbox: OutboxPolicy | None = None,
        audit: AuditLog | None = None,
        values: ValueSettings | None = None,
    ) -> None:
        self._store = store
        self._tracker = tracker
        self._lock = lock
        self._ids = ids
        self._registry = (
            dict(registry) if isinstance(registry, Mapping) else {d.name: d for d in registry}
        )
        self._settings = settings
        self._leaks = leak_policy
        self._sanitizer = sanitizer
        self._posture = posture
        self._outbox = outbox or OutboxPolicy()
        self._audit = audit
        self._values = values

    def _batch(self) -> AbstractContextManager[None]:
        """The lock's batch scope when it has one (:class:`BatchLock`), else nothing."""
        return self._lock.batch() if isinstance(self._lock, BatchLock) else nullcontext()

    # -- sync --------------------------------------------------------------------------------

    def sync(self, request: LabelSyncRequest) -> LabelSyncOutcome:
        """Converge every selected report's issue labels on its bead state by DRAFTING (see the
        module docstring). ``dry_run`` reads and reports, and writes nothing."""
        if not request.dry_run:
            if not self._outbox.enabled:
                raise IntakeRefused(
                    RefusalReason.OUTBOX_DISABLED,
                    "the outbox is off for this hive (intake.external.outbox.enabled; opt-in)",
                )
            machine.require_kind_enabled(OpKind.LABELS, self._outbox)
            require_private_store(self._posture)
        selected, skipped = self._select(request)
        facts = {view.id: self._facts(view) for view in selected}
        catalog = LabelCatalog.build(self._registry, self._settings, _observed(facts.values()))
        items = list(skipped)
        if selected:
            live = self._listing(include_closed=request.include_closed)
            defined = self._defined()
            with self._batch():  # one process: the lock may reuse its host check (bh-25fvb)
                for view in selected:
                    items.append(
                        self._guarded(
                            view.id,
                            lambda v=view: self._sync_one(
                                v, facts[v.id], catalog, live, defined, request
                            ),
                        )
                    )
        order = {report: n for n, report in enumerate(request.reports)}
        items.sort(key=lambda i: (order.get(i.report, len(order)), i.report))
        return LabelSyncOutcome(tuple(items), request.dry_run)

    # -- refresh (the on_transition hook) ----------------------------------------------------

    def refresh(self, reports: Iterable[str], actor: str) -> tuple[LabelSyncItem, ...]:
        """Draft or refresh the ONE ``labels`` record of each of ``reports`` from stored state,
        with no tracker call (see the module docstring). One item per distinct report, in order.
        Never raises: a gate that is off, an unbound report or a store failure is that item's
        ``note`` or ``error``, because the transition that called this has already happened."""
        wanted = list(dict.fromkeys(reports))
        try:
            if not self._outbox.enabled:
                raise IntakeRefused(
                    RefusalReason.OUTBOX_DISABLED,
                    "the outbox is off for this hive (intake.external.outbox.enabled; opt-in)",
                )
            machine.require_kind_enabled(OpKind.LABELS, self._outbox)
            require_private_store(self._posture)
        except IntakeRefused as exc:
            return tuple(LabelSyncItem(r, error=exc.detail or str(exc)) for r in wanted)
        return tuple(
            self._guarded(report, lambda r=report: self._refresh_one(r, actor)) for report in wanted
        )

    def _refresh_one(self, report: str, actor: str) -> LabelSyncItem:
        view = self._store.get(report)
        if view is None:
            return LabelSyncItem(report, error=f"{report} not found")
        binding = view.binding
        if binding is None:
            return LabelSyncItem(report, note="not bound to an external issue; no labels")
        if view.snapshot is None:
            return LabelSyncItem(
                report, binding.url, note="no stored issue snapshot; sync labels to read the issue"
            )
        facts = self._facts(view)
        catalog = LabelCatalog.build(self._registry, self._settings, _observed([facts]))
        base = self._stored_base(view, binding)
        desired = desired_labels(catalog, facts.report, facts.work, facts.progress)
        diff = diff_labels(catalog, desired, base.labels)
        swaps = tuple((dim, old, new) for dim, (old, new) in sorted(diff.swaps(catalog).items()))
        item = LabelSyncItem(view.id, binding.url, diff.add, diff.remove, diff.drift, swaps=swaps)
        live_type, live_fields = (
            self._believed(view.id, binding.url) if self._projects_values else (None, {})
        )
        item = self._with_values(item, facts, live_type or "", tuple(sorted(live_fields.items())))
        if not item.changed:
            return self._converged(view, binding, item, LabelSyncRequest(actor, reports=(report,)))
        unknown = {
            c.name
            for c in item.values
            if (live_type is None if c.name == TYPE_VALUE else c.name not in live_fields)
        }
        return self._draft(view, binding, base, item, actor, unknown=frozenset(unknown))

    def _with_values(
        self,
        item: LabelSyncItem,
        facts: _Facts,
        live_type: str | None,
        live_fields: tuple[tuple[str, str], ...] | None,
    ) -> LabelSyncItem:
        """``item`` with the issue type and field values to set and the notes of those skipped,
        for each value whose outbox gate is on (none without the maps)."""
        if not self._projects_values:
            return item
        wants_type, wants_fields = self._outbox.type_enabled, self._outbox.fields_enabled
        desired = desired_values(
            self._values, facts.report, facts.work, issue_type=wants_type, fields=wants_fields
        )
        typed, type_note = type_change(desired, live_type)
        fielded, field_note = field_changes(desired, live_fields)
        changes = ((typed,) if typed is not None else ()) + fielded
        notes = tuple(n for n in (*desired.notes, type_note, field_note) if n)
        return dataclasses.replace(item, values=changes, value_notes=notes)

    @property
    def _projects_values(self) -> bool:
        """The maps are bound and the outbox's ``issue.type`` or ``issue.fields`` gate is on."""
        return self._values is not None and (
            self._outbox.type_enabled or self._outbox.fields_enabled
        )

    def _believed(self, report: str, url: str) -> tuple[str | None, dict[str, str]]:
        """The issue's type and field values as the outbox believes them, for the tracker-free
        refresh: the base of the open labels record's value operation, else the values the last
        one sent set. A value nothing tells is absent (the type ``None``): it is drafted, never
        assumed held, and left out of the draft's base."""
        doc = self._store.read_outbox(report)
        known: dict[OpKind, dict[str, str]] = {OpKind.TYPE: {}, OpKind.FIELDS: {}}
        records = _labels_records(doc, url) if doc is not None else []
        for record in records:  # oldest first: a later record's knowledge wins
            for kind, held in known.items():
                op = record.op(kind)
                if op is None or op.values is None:
                    continue
                if op.state is OpState.SENT:
                    held.update(dict(op.values.values))
                elif record.status is RecordStatus.OPEN and op.state in _OPEN:
                    held.update(dict(op.values.base))
        return known[OpKind.TYPE].get(TYPE_VALUE), known[OpKind.FIELDS]

    def _stored_base(self, view: BeadView, binding: IssueBinding) -> LabelBase:
        """The labels the store believes the issue carries: the import snapshot's, with the owned
        ones replaced by those the open ``labels`` record was drafted on (a fresh read)."""
        snapshot = view.snapshot
        assert snapshot is not None
        base = LabelBase(snapshot.revision, frozenset(snapshot.labels))
        doc = self._store.read_outbox(view.id)
        open_id = _open_labels(doc, binding.url) if doc is not None else ""
        op = doc.records[open_id].op(OpKind.LABELS) if doc is not None and open_id else None
        if op is None or op.labels is None:
            return base
        kept = frozenset(label for label in base.labels if not self._leaks.owns(label))
        return LabelBase(op.labels.base_revision, kept | op.labels.base_owned)

    def _select(self, request: LabelSyncRequest) -> tuple[list[BeadView], list[LabelSyncItem]]:
        """The bound reports of this tracker project to sync, and an item for each named report
        that cannot be (missing, unbound, bound elsewhere)."""
        project = self._tracker.describe().project
        here = (project.tracker, project.locator)
        selected: list[BeadView] = []
        skipped: list[LabelSyncItem] = []
        if request.reports:
            views: list[BeadView] = []
            for report in dict.fromkeys(request.reports):
                view = self._store.get(report)
                if view is None:
                    skipped.append(LabelSyncItem(report, error=f"{report} not found"))
                elif view.binding is None:
                    skipped.append(
                        LabelSyncItem(report, note="not bound to an external issue; no labels")
                    )
                else:
                    views.append(view)
        else:
            views = [
                v
                for v in self._store.bound_reports()
                if request.include_closed or v.status != "closed"
            ]
        for view in views:
            binding = view.binding
            assert binding is not None
            if (binding.tracker, binding.project) != here:
                if request.reports:
                    skipped.append(
                        LabelSyncItem(
                            view.id,
                            binding.url,
                            note=f"bound to {binding.tracker}:{binding.project}, not this project",
                        )
                    )
                continue
            selected.append(view)
        return selected, skipped

    def _facts(self, view: BeadView) -> _Facts:
        report = ReportFacts.from_bead(view.labels, view.issue_type, view.priority or None)
        work: WorkFacts | None = None
        with store_session(self._store):  # the report's edges and outbox: one read (bh-25fvb)
            tracked = self._store.tracked_work(view.id)
            target = self._store.get(tracked) if tracked else None
            outbox = self._store.read_outbox(view.id)
        if target is not None:
            work = WorkFacts.from_bead(
                target.status, target.labels, target.issue_type, target.priority or None
            )
        return _Facts(report, work, progress_facts(outbox), outbox)

    def _listing(self, *, include_closed: bool) -> dict[str, TrackerIssue]:
        try:
            return issue_index(self._tracker, include_closed=include_closed)
        except TrackerError as exc:
            raise IntakeRefused(
                RefusalReason.TRACKER_ERROR,
                f"the issue listing failed ({type(exc).__name__}); nothing was drafted",
            ) from exc

    def _defined(self) -> frozenset[str] | None:
        """The repository's label names (ONE ``labels.read``); ``None`` when they cannot be read,
        so no add can be shown to be defined (every add is refused)."""
        if not isinstance(self._tracker, LabelReader):
            return None
        try:
            return frozenset(d.name for d in self._tracker.list_label_definitions())
        except TrackerError:
            return None

    def _sync_one(
        self,
        view: BeadView,
        facts: _Facts,
        catalog: LabelCatalog,
        live: Mapping[str, TrackerIssue],
        defined: frozenset[str] | None,
        request: LabelSyncRequest,
    ) -> LabelSyncItem:
        binding = view.binding
        assert binding is not None
        issue = live.get(binding.url)
        if issue is None:
            note = (
                "the issue is not in the listing (deleted or moved)"
                if request.include_closed
                else "the issue is not open (closed: sync with include_closed)"
            )
            return LabelSyncItem(view.id, binding.url, note=note)
        desired = desired_labels(catalog, facts.report, facts.work, facts.progress)
        diff = diff_labels(catalog, desired, issue.labels)
        add = diff.add
        remove = diff.remove | (diff.drift if request.prune_unmapped else frozenset())
        swaps = tuple((dim, old, new) for dim, (old, new) in sorted(diff.swaps(catalog).items()))
        item = LabelSyncItem(view.id, binding.url, add, remove, diff.drift, swaps=swaps)
        item = self._with_values(item, facts, issue.issue_type, issue.fields)
        if not item.changed:
            return self._converged(view, binding, item, request, known=facts)
        undefined = frozenset(a for a in add if defined is None or a not in defined)
        if undefined:
            why = (
                "the repository's label definitions could not be read (labels.read)"
                if defined is None
                else "the repository does not define them"
            )
            return dataclasses.replace(
                item,
                undefined=undefined,
                error=(
                    f"would add {', '.join(sorted(undefined))}, but {why}; run `{DEFINE_VERB}` "
                    "first (nothing drafted)"
                ),
            )
        if request.dry_run:
            return item
        return self._draft(view, binding, label_base(issue), item, request.actor)

    def _draft(
        self,
        view: BeadView,
        binding: IssueBinding,
        base: LabelBase,
        item: LabelSyncItem,
        actor: str,
        *,
        unknown: frozenset[str] = frozenset(),
    ) -> LabelSyncItem:
        """O1 of ONE labels record under the binding's lock: O2 when the newest open one already
        carries this diff on these owned labels, else a new one superseding the older (O6).
        ``unknown`` names the values whose live value was not known (left out of the base)."""
        owned = frozenset(label for label in base.labels if self._leaks.owns(label))
        issue_type, fields = _value_intents(item.values, unknown)
        with held_binding(self._lock, self._store, binding):
            doc = self._store.read_outbox(view.id) or OutboxDocument()
            flying = _in_flight(doc, binding.url)
            if flying:
                return dataclasses.replace(
                    item,
                    note=f"label change {flying} may be in flight; reconcile it before a new draft",
                )
            intent = LabelIntent(item.add, item.remove)
            revision = labels_revision(
                doc,
                binding.url,
                intent,
                base_owned=owned,
                issue_type=issue_type,
                fields=fields,
            )
            applied = machine.draft(
                doc,
                DraftRequest(
                    binding,
                    OutboxEvent.LABELS,
                    revision,
                    actor,
                    comment=False,
                    labels=intent if item.labels_changed else None,
                    issue_type=issue_type,
                    fields=fields,
                ),
                outbox_id=self._ids.outbox_id(),
                policy=self._outbox,
                ctx=self._ctx(view, binding, base),
            )
            if not applied.changed:  # O2: this very diff is already drafted
                return dataclasses.replace(item, pending=applied.outbox_id)
            self._store.write_outbox(view.id, applied.document, expect_seq=doc.seq, actor=actor)
        record = applied.record
        ops = record.operations if record is not None else ()
        return dataclasses.replace(
            item,
            drafted=applied.outbox_id,
            superseded=(record.supersedes or "") if record is not None else "",
            held=any(op.state is OpState.HELD for op in ops),
        )

    def _converged(
        self,
        view: BeadView,
        binding: IssueBinding,
        item: LabelSyncItem,
        request: LabelSyncRequest,
        *,
        known: _Facts | None = None,
    ) -> LabelSyncItem:
        """Nothing to change. An open labels record left from an earlier diff is no longer
        needed: an operator's (non-dry) sync discards it (O8), anyone else is told. ``known``
        is this run's facts of the report, whose outbox read is reused for the check."""
        doc = known.outbox if known is not None else self._store.read_outbox(view.id)
        moot = _open_labels(doc, binding.url) if doc is not None else ""
        if not moot:
            return item
        if request.dry_run or not machine.is_operator_seat(request.actor, self._outbox):
            return dataclasses.replace(
                item, note=f"open labels record {moot} is no longer needed; discard it"
            )
        with held_binding(self._lock, self._store, binding):
            doc = self._store.read_outbox(view.id) or OutboxDocument()
            moot = _open_labels(doc, binding.url)
            if not moot:
                return item
            current = doc
            for op in doc.records[moot].operations:  # labels, type and fields: each still open
                if op.state not in _OPEN:
                    continue
                current = machine.discard(
                    current,
                    moot,
                    reason="the issue already carries the projected labels and values",
                    actor=request.actor,
                    policy=self._outbox,
                    kind=op.kind,
                ).document
            self._store.write_outbox(view.id, current, expect_seq=doc.seq, actor=request.actor)
        return dataclasses.replace(item, discarded=moot)

    def _ctx(self, view: BeadView, binding: IssueBinding, base: LabelBase) -> RenderContext:
        thread = self._store.read_thread(view.id)
        return RenderContext(
            binding.url,
            self._leaks,
            self._sanitizer,
            labels=base,
            thread_missing=thread is not None and thread.state is ThreadState.MISSING,
        )

    @staticmethod
    def _guarded(report: str, step) -> LabelSyncItem:
        """One report's failure is its item's error; the next report is still synced."""
        try:
            return step()
        except (IntakeRefused, IntakeStoreError) as exc:
            return LabelSyncItem(report, error=str(exc))

    # -- define ------------------------------------------------------------------------------

    def define(self, request: LabelDefineRequest) -> LabelDefineOutcome:
        """The definitions plan and its digest; with ``approve``, apply it (see the module
        docstring). Refuses to apply a plan whose digest moved since it was reviewed, that a
        leak finding or a tracker limit blocks, or for anyone but an operator seat."""
        if request.dry_run and request.approve:
            raise IntakeRefused(
                RefusalReason.INVALID_REQUEST, "a dry run approves nothing; drop one of them"
            )
        tracker = self._tracker
        if not isinstance(tracker, LabelReader):
            raise IntakeRefused(
                RefusalReason.CAPABILITY_UNUSABLE,
                "the bound tracker adapter cannot read label definitions (labels.read)",
            )
        approving = bool(request.approve)
        if approving:
            if not machine.is_operator_seat(request.actor, self._outbox):
                raise IntakeRefused(
                    RefusalReason.OPERATOR_SEAT_REQUIRED,
                    f"defining labels needs a human operator seat, not {request.actor!r}",
                )
            self._require_definer(request.prune)
        plan = self._plan(request.prune)
        if not approving:
            return LabelDefineOutcome(plan, detail=_plan_detail(plan))
        if not machine.digest_matches(request.approve, plan.digest):
            raise IntakeRefused(
                RefusalReason.STALE_DIGEST,
                f"the plan is now {plan.short_digest}; review it again before approving",
            )
        if plan.findings:
            raise IntakeRefused(
                RefusalReason.UNRESOLVED_FINDING,
                f"leak findings in {', '.join(sorted(plan.findings))}; fix the settings",
            )
        if plan.problems:
            raise IntakeRefused(
                RefusalReason.INVALID_REQUEST,
                "; ".join(f"{p.name}: {p.detail}" for p in plan.problems),
            )
        if not plan.changed:
            return LabelDefineOutcome(plan, detail="already converged; nothing to apply")
        return self._apply(plan, request.actor)

    def _require_definer(self, prune: bool) -> None:
        """The writes the apply needs, before any request: the writer protocols, then one probe."""
        if not isinstance(self._tracker, LabelDefiner):
            raise IntakeRefused(
                RefusalReason.CAPABILITY_UNUSABLE,
                "the bound tracker adapter cannot define labels (labels.define)",
            )
        if prune and not isinstance(self._tracker, LabelPruner):
            raise IntakeRefused(
                RefusalReason.CAPABILITY_UNUSABLE,
                "the bound tracker adapter cannot delete label definitions (prune)",
            )
        states = self._tracker.probe_capabilities().states
        needed = [Operation.LABELS_READ, Operation.LABELS_DEFINE]
        if prune:
            needed.append(Operation.ISSUES_READ)
        missing = [op.value for op in needed if op not in states or not states[op].usable]
        if missing:
            raise IntakeRefused(
                RefusalReason.CAPABILITY_UNUSABLE,
                f"not usable on this tracker: {', '.join(missing)}",
            )

    def _plan(self, prune: bool) -> LabelDefinePlan:
        tracker = self._tracker
        assert isinstance(tracker, LabelReader)
        catalog = LabelCatalog.build(
            self._registry,
            self._settings,
            _observed(self._facts(v) for v in self._store.bound_reports()),
        )
        try:
            live = tuple(
                LiveLabel(d.name, d.color, d.description) for d in tracker.list_label_definitions()
            )
            carried = self._carried() if prune else {}
        except TrackerError as exc:
            raise IntakeRefused(
                RefusalReason.TRACKER_ERROR,
                f"the label definitions could not be read ({type(exc).__name__})",
            ) from exc
        project = tracker.describe().project
        return plan_definitions(
            catalog,
            live,
            target=f"{project.tracker}:{project.locator}",
            policy=self._leaks,
            sanitizer=self._sanitizer,
            prune=prune,
            carried=carried,
        )

    def _carried(self) -> dict[str, tuple[str, ...]]:
        """Every owned label an OPEN issue carries, with those issues (ONE open listing)."""
        out: dict[str, list[str]] = {}
        for issue in issue_index(self._tracker, include_closed=False).values():
            for label in issue.labels:
                if self._settings.owns(label):
                    out.setdefault(label, []).append(issue.key.identifier)
        return {label: tuple(sorted(keys)) for label, keys in out.items()}

    def _apply(self, plan: LabelDefinePlan, actor: str) -> LabelDefineOutcome:
        """The writes of an approved plan: ONE grant for the create/update write, one for the
        delete write; each grant handed to the adapter once. Then the local audit note."""
        tracker = self._tracker
        assert isinstance(tracker, LabelDefiner)
        project = tracker.describe().project
        grants: list[str] = []
        writes = tuple(
            LabelDefinition(s.name, s.color, s.description) for s in (*plan.create, *plan.update)
        )
        if writes:
            grant = WriteGrant(self._ids.grant_id(), Operation.LABELS_DEFINE, project)
            grants.append(grant.grant_id)
            tracker.define_labels(writes, grant=grant)
        if plan.delete:
            assert isinstance(tracker, LabelPruner)
            grant = WriteGrant(self._ids.grant_id(), Operation.LABELS_DEFINE, project)
            grants.append(grant.grant_id)
            tracker.delete_labels(plan.delete, grant=grant)
        note = _audit_note(plan, actor, grants)
        if self._audit is not None:
            self._audit.note(note, actor=actor)
        return LabelDefineOutcome(plan, True, tuple(grants), note, _plan_detail(plan, applied=True))


def _observed(facts: Iterable[_Facts]) -> dict[str, set[str]]:
    """The open-set values on the projected beads: components (work and report) and tags."""
    observed: dict[str, set[str]] = {COMPONENT_DIM: set(), TAG_DIM: set()}
    for f in facts:
        observed[COMPONENT_DIM].update(f.report.components)
        observed[TAG_DIM].update(f.report.tags)
        if f.work is not None:
            observed[COMPONENT_DIM].update(f.work.components)
    return observed


def _labels_records(doc: OutboxDocument, url: str):
    return sorted(
        (
            r
            for r in doc.records.values()
            if r.event is OutboxEvent.LABELS and r.draft_key.binding_url == url
        ),
        key=lambda r: r.created_seq,
    )


def _in_flight(doc: OutboxDocument, url: str) -> str:
    """A labels record any of whose operations (labels, type, fields) may be in flight."""
    for record in _labels_records(doc, url):
        if any(op.state in _IN_FLIGHT for op in record.operations):
            return record.outbox_id
    return ""


def _open_labels(doc: OutboxDocument, url: str) -> str:
    """The open labels record with an operation still waiting to be approved or sent, if any."""
    for record in reversed(_labels_records(doc, url)):
        if record.status is RecordStatus.OPEN and any(
            op.state in _OPEN for op in record.operations
        ):
            return record.outbox_id
    return ""


def _value_intents(
    changes: Iterable[ValueChange], unknown: frozenset[str] = frozenset()
) -> tuple[ValueIntent | None, ValueIntent | None]:
    """The ``issue.type`` and ``issue.fields`` intents of a sync item's value changes, each
    stamped with the live values it was drafted on (an ``unknown`` one is left out of the
    base, so the batch never calls it stale)."""
    typed = [c for c in changes if c.name == TYPE_VALUE]
    fielded = [c for c in changes if c.name != TYPE_VALUE]

    def intent(group: list[ValueChange]) -> ValueIntent | None:
        if not group:
            return None
        return ValueIntent(
            tuple((c.name, c.desired) for c in group),
            tuple((c.name, c.live) for c in group if c.name not in unknown),
        )

    return intent(typed), intent(fielded)


def _plan_detail(plan: LabelDefinePlan, *, applied: bool = False) -> str:
    counts = f"{len(plan.create)} to create, {len(plan.update)} to update"
    counts += f", {len(plan.delete)} to delete" if plan.delete else ""
    if plan.kept:
        counts += f"; {len(plan.kept)} kept (an open issue carries them)"
    if applied:
        return f"applied {plan.short_digest}: {counts}"
    if not plan.changed:
        return "converged: the repository defines every projected label"
    if plan.blocked:
        return f"{counts}; blocked until the findings and problems are fixed"
    return f"{counts}; approve {plan.short_digest} to apply"


def _audit_note(plan: LabelDefinePlan, actor: str, grants: list[str]) -> str:
    parts = [f"labels define {plan.short_digest} on {plan.target} approved by {actor}"]
    for verb, names in (
        ("created", [s.name for s in plan.create]),
        ("updated", [s.name for s in plan.update]),
        ("deleted", list(plan.delete)),
    ):
        if names:
            parts.append(f"{verb} {', '.join(names)}")
    parts.append(f"grants {', '.join(grants)}")
    return "; ".join(parts)


__all__ = ["LabelProjection", "issue_index", "label_base"]
