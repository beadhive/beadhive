"""``IssueImporter``: import one external issue as an untriaged, held internal report (I4).

The operation behind ``bh work import <issue-url>`` (external-intake-module-adr.md section 5,
decisions D1 and D3). The report bead is keyed on the CANONICAL issue URL the tracker returns,
looked up in the operational store only, so importing the same URL twice updates and never
duplicates. This layer is the only tracker-typed code of import (resolution X2): it resolves the
URL through the optional :class:`IssueResolver` (R3 to R7 distinct) or, without one, pages
``list_issues`` and matches the URL (R3 to R6 collapse into ``NOT_FOUND_IN_PROJECT``), then maps
the issue to the module's own records.

Import is READ-ONLY toward the tracker: it calls ``resolve``, ``get`` and ``list_issues`` and
nothing else, holds no ``WriteGrant`` and reaches no writer. The issue body is untrusted input,
stored as data and never interpreted. Refusals R0 to R8 are decided before the first store write;
a dry run reaches the same decision and then stops, with zero store writes.

Create order (one report, no tracker write): ``external_ref`` and ``intake.source`` with the
create, the ``intake-hold:`` gate at once (the store's create), then ``origin``, then
``intake=untriaged``. A found report is repaired for a missing piece only and its snapshot is
rewritten only for a newer revision; title, description, ``intake`` value, status and links are
never touched, so a re-import never resets triage.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from beadhive.modules.tracker.contracts import (
    ChangeRequestUrl,
    IssueKey,
    IssueQuery,
    IssueResolver,
    NotVisible,
    OperationNotEnabled,
    OtherProject,
    Revision,
    TrackerError,
    TrackerIssue,
    TrackerReader,
    UrlNotRecognized,
)

from ..contracts import IntakeLock, LabelPolicy, ReportStore
from ..domain import (
    INTAKE_DIM,
    INTAKE_UNTRIAGED,
    ORIGIN_GITHUB,
    REPORT_TYPES,
    BeadView,
    ImportKind,
    ImportOutcome,
    ImportRequest,
    IntakeRefused,
    IntakeStoreError,
    IssueBinding,
    IssueStatus,
    ReferenceShape,
    RefusalReason,
    ReportDraft,
    SourceSnapshot,
    channel_of,
    classify_reference,
    revision_is_newer,
)
from .paging import iterate_pages
from .threads import binding_key

#: The repair names an :class:`ImportOutcome` may report, in the order they are applied.
REPAIR_HOLD = "hold"
REPAIR_ORIGIN = "origin"
REPAIR_INTAKE = "intake"
REPAIR_SOURCE = "source"


def revision_token(revision: Revision) -> str:
    """The module's opaque revision token: the TRACKER's ``updated_at`` (never the local clock)
    plus its version when it has one, ``<iso8601>[#<version>]``."""
    stamp: datetime = revision.updated_at
    token = stamp.isoformat()
    return f"{token}#{revision.version}" if revision.version else token


def snapshot_of(issue: TrackerIssue) -> SourceSnapshot:
    """The issue as the module's own snapshot record (stored as ``intake.source``)."""
    author = issue.author
    return SourceSnapshot(
        title=issue.title,
        body=issue.body,
        state=IssueStatus(issue.state.value),
        revision=revision_token(issue.revision),
        closed_reason=issue.closed_reason or "",
        labels=tuple(sorted(issue.labels)),
        author_id=author.key.id if author is not None else "",
        author_kind=author.key.kind.value if author is not None else "unknown",
    )


def _comparable(url: str) -> str:
    """A URL without query, fragment or trailing slash: the fallback's only canonicalisation, the
    generic part. Anything tracker-specific (case, aliases) belongs to a resolver."""
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _tracker_error(exc: TrackerError) -> IntakeRefused:
    """A tracker failure the import cannot classify further. The message is already scrubbed of
    credentials by the adapter that raised it."""
    return IntakeRefused(
        RefusalReason.TRACKER_ERROR,
        f"the tracker could not answer ({type(exc).__name__}): {exc}",
    )


def _disabled_read(exc: OperationNotEnabled) -> IntakeRefused:
    return IntakeRefused(
        RefusalReason.INTAKE_DISABLED,
        f"issues.read is not enabled for this hive's tracker ({exc}); enable it on the host",
    )


class IssueImporter:
    """Implements ``IntakePort.import_issue`` over one store and one tracker.

    ``origin`` is the ``origin:<channel>`` label of the bound tracker (the composition root names
    it from the bound adapter; ``origin:github`` is the only tracker today, never
    ``origin:import``). ``default_type`` is the issue type of a new report
    (``intake.external.default_type``). Every mutation runs under the binding's
    :class:`IntakeLock`; a dry run only reads."""

    def __init__(
        self,
        store: ReportStore,
        labels: LabelPolicy,
        tracker: TrackerReader,
        lock: IntakeLock,
        *,
        origin: str = ORIGIN_GITHUB,
        default_type: str = "bug",
    ) -> None:
        self._store = store
        self._labels = labels
        self._tracker = tracker
        self._lock = lock
        self._origin = origin
        self._default_type = default_type

    # -- the operation -----------------------------------------------------------------------

    def import_issue(self, request: ImportRequest) -> ImportOutcome:
        reference = self._reference(request.reference)
        issue = self._resolve(reference)
        project = self._tracker.describe().project
        binding = IssueBinding(
            "", project.tracker, project.locator, issue.key.url, issue.key.identifier
        )
        if request.dry_run:
            return self._decide(issue, binding)[0]
        with self._lock.hold(binding_key(binding)):
            return self._upsert(issue, binding, request.actor)

    # -- refusals before any tracker request (R1, R2) ------------------------------------------

    def _reference(self, text: str) -> str:
        shape = classify_reference(text, public_prefixes=self._store.scope().public_prefixes)
        if shape is ReferenceShape.PUBLIC_STORE_ID:
            raise IntakeRefused(
                RefusalReason.PUBLIC_STORE_ID,
                "public-store ids are minted per host; import by the issue URL",
            )
        if shape is not ReferenceShape.URL:
            raise IntakeRefused(
                RefusalReason.NOT_A_URL,
                "import takes the external issue's URL, not a bead id or an issue number",
            )
        return text.strip()

    # -- the tracker side (R3 to R7, or the fallback's NOT_FOUND_IN_PROJECT) ----------------------

    def _resolve(self, reference: str) -> TrackerIssue:
        try:
            if isinstance(self._tracker, IssueResolver):
                return self._through_resolver(self._tracker, reference)
            return self._through_listing(reference)
        except OperationNotEnabled as exc:
            raise _disabled_read(exc) from exc
        except TrackerError as exc:
            raise _tracker_error(exc) from exc

    def _through_resolver(self, resolver: IssueResolver, reference: str) -> TrackerIssue:
        try:
            key = resolver.resolve(reference)  # pure: R3, R4 and the URL form of R5
            issue = resolver.get(key)  # one read: R6, and the tracker's answer for R5
        except UrlNotRecognized as exc:
            raise IntakeRefused(
                RefusalReason.UNKNOWN_URL,
                f"not an issue URL of this hive's tracker: {exc}; paste the issue's own URL",
            ) from exc
        except OtherProject as exc:
            raise IntakeRefused(
                RefusalReason.OTHER_PROJECT,
                f"that issue belongs to another project: {exc}; import it from that hive",
            ) from exc
        except ChangeRequestUrl as exc:
            raise IntakeRefused(
                RefusalReason.CHANGE_REQUEST,
                f"that is a change request, not an issue: {exc}; import the issue it fixes",
            ) from exc
        except NotVisible as exc:
            raise IntakeRefused(
                RefusalReason.NOT_VISIBLE,
                f"the issue is missing or hidden from this credential: {exc}",
            ) from exc
        if issue.key.url != key.url:
            self._require_same_project(resolver, issue.key)
        return issue

    @staticmethod
    def _require_same_project(resolver: IssueResolver, moved: IssueKey) -> None:
        """R7: the canonical URL the tracker answered differs from the one asked for. A transfer
        puts it in another project (refused); a rename inside the project is fine and the new
        canonical URL becomes the key."""
        try:
            resolver.resolve(moved.url)
        except (OtherProject, UrlNotRecognized) as exc:
            raise IntakeRefused(
                RefusalReason.OTHER_PROJECT,
                "that issue was transferred to another project; import it from that hive",
            ) from exc

    def _through_listing(self, reference: str) -> TrackerIssue:
        """Without an ``IssueResolver``: page ``list_issues`` (closed included) for the URL."""
        wanted = _comparable(reference)
        for page in iterate_pages(
            lambda cursor: self._tracker.list_issues(IssueQuery(cursor=cursor))
        ):
            for issue in page.items:
                if _comparable(issue.key.url) == wanted:
                    return issue
        raise IntakeRefused(
            RefusalReason.NOT_FOUND_IN_PROJECT,
            "no issue with that URL was found in this hive's project (it may be another project, "
            "a change request, hidden from the credential or not an issue URL)",
        )

    # -- the store side: idempotence on the canonical URL, R8 ------------------------------------

    def _existing(self, url: str) -> BeadView | None:
        found = self._store.find_by_external_ref(url)
        if len(found) > 1:
            raise IntakeRefused(
                RefusalReason.DUPLICATE_BINDING,
                f"{url} is bound by {', '.join(sorted(view.id for view in found))}; close all but "
                "one of them, then import again (nothing was written)",
            )
        return found[0] if found else None

    def _decide(
        self, issue: TrackerIssue, binding: IssueBinding
    ) -> tuple[ImportOutcome, BeadView | None, bool]:
        """Classify against the operational store, reading only. Returns the outcome (as a dry run
        reads it), the found report and whether its snapshot is to be rewritten."""
        view = self._existing(binding.url)
        if view is None:
            return ImportOutcome(ImportKind.CREATED, binding, dry_run=True), None, False
        bound = replace(binding, report=view.id)
        repairs = self._missing(view)
        snapshot = snapshot_of(issue)
        refresh = view.snapshot is not None and revision_is_newer(
            view.snapshot.revision, snapshot.revision
        )
        kind = ImportKind.UPDATED if repairs or refresh else ImportKind.UNCHANGED
        return ImportOutcome(kind, bound, dry_run=True, repaired=repairs), view, refresh

    def _missing(self, view: BeadView) -> tuple[str, ...]:
        """The pieces an interrupted earlier create left out (never an existing disposition)."""
        missing: list[str] = []
        if view.status != "closed" and not any(
            view.id in hold.blocks for hold in self._store.holds()
        ):
            missing.append(REPAIR_HOLD)
        if channel_of(view.labels, view.source_system) is None:
            missing.append(REPAIR_ORIGIN)
        if not any(label.startswith(f"{INTAKE_DIM}:") for label in view.labels):
            missing.append(REPAIR_INTAKE)
        if view.snapshot is None:
            missing.append(REPAIR_SOURCE)
        return tuple(missing)

    def _upsert(self, issue: TrackerIssue, binding: IssueBinding, actor: str) -> ImportOutcome:
        decided, view, refresh = self._decide(issue, binding)
        snapshot = snapshot_of(issue)
        if view is None:
            report = self._create(issue, binding, snapshot, actor)
            return ImportOutcome(ImportKind.CREATED, replace(binding, report=report))
        if decided.kind is ImportKind.UNCHANGED:
            return ImportOutcome(ImportKind.UNCHANGED, decided.binding)
        self._repair(view, decided.repaired, snapshot, actor)
        if refresh:
            self._write(view.id, "record_snapshot", actor, snapshot=snapshot)
        return ImportOutcome(ImportKind.UPDATED, decided.binding, repaired=decided.repaired)

    def _create(
        self, issue: TrackerIssue, binding: IssueBinding, snapshot: SourceSnapshot, actor: str
    ) -> str:
        if self._default_type not in REPORT_TYPES:
            raise IntakeRefused(
                RefusalReason.INVALID_TYPE,
                f"intake.external.default_type must be one of {', '.join(sorted(REPORT_TYPES))} "
                f"(got {self._default_type!r})",
            )
        new_labels = [*self._store.scope().hive.labels(), self._origin, INTAKE_UNTRIAGED]
        bad = self._labels.violations(new_labels)
        if bad:
            raise IntakeRefused(
                RefusalReason.INVALID_LABELS,
                "report bead would carry invalid labels: " + "; ".join(bad),
            )
        draft = ReportDraft(
            title=issue.title or issue.key.identifier,
            issue_type=self._default_type,
            origin=self._origin,
            actor=actor,
            description=issue.body,
            binding=binding,
            snapshot=snapshot,
        )
        report = self._store.create(draft)  # external_ref and intake.source, then the hold
        for dimension, value in (_split(self._origin), _split(INTAKE_UNTRIAGED)):
            self._stamp(report, dimension, value, actor)
        # a second row for the URL (a racing create on a replica) is refused, never auto-closed
        self._existing(binding.url)
        return report

    def _repair(
        self, view: BeadView, repairs: tuple[str, ...], snapshot: SourceSnapshot, actor: str
    ) -> None:
        for repair in repairs:
            if repair == REPAIR_HOLD:
                self._write(view.id, "open_hold", actor)
            elif repair == REPAIR_ORIGIN:
                self._stamp(view.id, *_split(self._origin), actor)
            elif repair == REPAIR_INTAKE:
                self._stamp(view.id, *_split(INTAKE_UNTRIAGED), actor)
            elif repair == REPAIR_SOURCE:
                self._write(view.id, "record_snapshot", actor, snapshot=snapshot)

    def _stamp(self, report: str, dimension: str, value: str, actor: str) -> None:
        try:
            self._store.set_state(
                report, dimension, value, reason="imported from the external tracker", actor=actor
            )
        except IntakeStoreError as exc:
            # The report exists (and is held): carry its id so the caller can name a half-import.
            raise IntakeStoreError(
                exc.detail, bead=report, operation=f"stamp:{dimension}", returncode=exc.returncode
            ) from exc

    def _write(self, report: str, operation: str, actor: str, *, snapshot=None) -> None:
        try:
            if operation == "open_hold":
                self._store.open_hold(report, actor=actor)
            else:
                self._store.record_snapshot(report, snapshot, actor=actor)
        except IntakeStoreError as exc:
            raise IntakeStoreError(
                exc.detail, bead=report, operation=operation, returncode=exc.returncode
            ) from exc


def _split(label: str) -> tuple[str, str]:
    dimension, _, value = label.partition(":")
    return dimension, value


__all__ = [
    "REPAIR_HOLD",
    "REPAIR_INTAKE",
    "REPAIR_ORIGIN",
    "REPAIR_SOURCE",
    "IssueImporter",
    "revision_token",
    "snapshot_of",
]
