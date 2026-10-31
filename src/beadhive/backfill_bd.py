"""The bd StoreLoader: BeadLog events into a bd hive, with every degradation reported.

Loader #1 of ``docs/design/backfill-platform-adr.md`` (section 5), following the bd/Dolt mapping
and loss-report contract of ``docs/design/beadlog-format-adr.md`` (sections 8 and 9). It
implements ``beadhive_backfill``'s :class:`StoreLoader`, :class:`StoreReader` and
:class:`PreStateExporter` ports; the hive apply envelope (``BackfillEngine``) drives it.

How a load works (one :meth:`BdStoreLoader.load` call):

1. **One bulk read.** ``bd export --all`` once (:func:`beadhive.bd.export_jsonl`), read back as
   events by the neutral reader (``beadlog_sources.bd``), which re-emits the events this loader
   stored earlier with their original ids.
2. **Fold, don't replay.** Per bead, the target state is the BeadLog fold of the store's events
   plus the batch; the writes are the difference between that and the current state. A batch
   event that decides no facet (a later event of the same facet wins) is reported
   ``x-bd.superseded`` and not written, because bd keeps current state only. Loading the same
   events again finds nothing to change and writes nothing.
3. **Batched writes.** New beads land in ONE ``bd import`` through :func:`bd.import_labeled`
   (which injects and validates the identity triplet), carrying ``created_at`` / ``closed_at`` /
   ``started_at`` and dependency and comment ``created_at`` from the deciding events — those are
   the times ``bd import`` honors (measured on bd 1.3.0). An existing bead gets ONE ``bd update``
   (fields, labels, non-closing status, metadata document), then ``bd close --force`` /
   ``bd dep`` / ``bd comments add`` as needed.
4. **Occurrence time in metadata.** Each applied event's identity (``event_id``,
   ``occurred_at``, ``recorded_at``, source, provenance, actor, ...) is kept in
   ``metadata.bdlg.<facet>`` (``beadlog_sources.bd.facet_key`` / ``envelope``).
5. **Loss report.** Every load, dry run or apply, returns one report validating against
   ``docs/schemas/beadlog/v0.1/loss-report.schema.json``; a dry run and the apply of the same
   events against the same store report the same losses.

Safety: undoing a ``create`` is a ``retire`` (``bd close`` with reason ``backfill-rollback``),
never a delete. The ``delete`` kind and bead-scope ``tombstone`` erasure need
``delete_enabled=True`` (off by default); without it they are reported dropped and nothing is
deleted. The loader never runs ``bd compact``/``flatten`` or Dolt GC.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from beadlog import (
    FORMAT_VERSION,
    BeadEvent,
    BeadRef,
    BeadState,
    Edge,
    EventKind,
    LoaderCapability,
    fold,
    format_timestamp,
    mint_event,
    ordering_key,
    union_merge,
)
from beadlog_sources.bd import (
    CLOSED,
    FIELD_COLUMNS,
    METADATA_FIELD_PREFIX,
    METADATA_KEY,
    RETIRED,
    RETIRED_KEY,
    TIMESTAMP_FIELDS,
    baseline_events,
    envelope,
    facet_key,
    parse_export,
    row_events,
    row_state,
)

from . import bd
from .identity import resolve_actor, workspace_identity

LOADER_NAME = "bd"
LOADER_VERSION = "0.1.0"
LOSS_REPORT_VERSION = "0.1.0"
CAPABILITIES = frozenset({str(LoaderCapability.METADATA_FALLBACK)})
DEFAULT_STATUSES = ("open", "in_progress", "blocked", "deferred", "closed")
ORIGIN_LABEL = "origin:backfill"
STAMP_SYSTEM = "beadhive-backfill"

# BeadLog field -> its ``bd update`` flag. ``owner`` has none: bd honors it only at import.
UPDATE_FLAGS: Mapping[str, str] = {
    "title": "title",
    "description": "description",
    "design": "design",
    "acceptance_criteria": "acceptance",
    "notes": "notes",
    "priority": "priority",
    "assignee": "assignee",
    "external_ref": "external-ref",
    "due_at": "due",
    "defer_until": "defer",
    "estimated_minutes": "estimate",
    "spec_id": "spec-id",
    "type": "type",
}
IMPORT_ONLY_FIELDS = frozenset({"owner"})
UNCLEARABLE_FIELDS = frozenset({"title", "priority", "estimated_minutes", "type"})
INTEGER_FIELDS = frozenset({"priority", "estimated_minutes"})
_SOURCE_KIND = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")

Clock = Callable[[], str]


def system_clock() -> str:
    """The wall clock as a canonical BeadLog timestamp."""
    return format_timestamp(datetime.now(UTC))


class BdLoadError(RuntimeError):
    """A bd read or write the loader depends on failed."""


# -- the store port ------------------------------------------------------------------------


class BdPort(Protocol):
    """The bd operations the loader needs. :class:`BdCliPort` is the real one; tests substitute
    an in-memory bd."""

    @property
    def target(self) -> str: ...

    @property
    def identity_labels(self) -> tuple[str, ...]:
        """Labels ``bd import`` adds to every new bead (the hive's identity triplet)."""
        ...

    def export_rows(self) -> list[dict[str, Any]]: ...

    def export_to(self, path: Path) -> None: ...

    def import_records(self, records: Sequence[Mapping[str, Any]]) -> None: ...

    def apply_changes(
        self,
        bead: str,
        *,
        fields: Mapping[str, str],
        add_labels: Sequence[str],
        remove_labels: Sequence[str],
        status: str,
        metadata: Mapping[str, Any] | None,
        force: bool,
    ) -> None: ...

    def unset_metadata(self, bead: str, keys: Sequence[str]) -> None: ...

    def close(self, bead: str, reason: str) -> None: ...

    def add_dependency(self, bead: str, target: str, type_: str) -> None: ...

    def remove_dependency(self, bead: str, target: str) -> None: ...

    def add_comment(self, bead: str, body: str, author: str | None) -> None: ...

    def delete(self, bead: str) -> None: ...


def _check(result: Any, what: str) -> None:
    if result.returncode != 0:
        raise BdLoadError(f"{what} failed: {bd.err_detail(result)}")


class BdCliPort:
    """:class:`BdPort` over the hive at ``cwd`` through ``beadhive.bd``'s named routes."""

    def __init__(self, cwd: Path | str, *, actor: str) -> None:
        self.cwd = Path(cwd)
        self.actor = actor
        self._routes = bd.routes(self.cwd)

    @property
    def target(self) -> str:
        return f"bd:{self.cwd}"

    @property
    def identity_labels(self) -> tuple[str, ...]:
        ident = workspace_identity(self.cwd)
        if ident is None:
            return ()
        provider, org, repo = ident
        return (f"provider:{provider}", f"org:{org}", f"repo:{repo}")

    def export_rows(self) -> list[dict[str, Any]]:
        text = bd.export_jsonl(self.cwd, all_=True)
        if text is None:
            raise BdLoadError("bd export failed; nothing was written")
        return parse_export(text)

    def export_to(self, path: Path) -> None:
        if bd.export_jsonl(self.cwd, all_=True, output=path) is None:
            raise BdLoadError(f"bd export -o {path} failed")

    def import_records(self, records: Sequence[Mapping[str, Any]]) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
            handle.write("".join(json.dumps(dict(record)) + "\n" for record in records))
            source = handle.name
        try:
            # one token: import_labeled reads every non-flag argument as the source path
            code, error = bd.import_labeled([f"--actor={self.actor}", source], self.cwd)
        finally:
            Path(source).unlink(missing_ok=True)
        if code != 0:
            raise BdLoadError(f"bd import failed: {error or f'exit {code}'}")

    def apply_changes(
        self,
        bead: str,
        *,
        fields: Mapping[str, str],
        add_labels: Sequence[str],
        remove_labels: Sequence[str],
        status: str,
        metadata: Mapping[str, Any] | None,
        force: bool,
    ) -> None:
        result = self._routes.issue_apply_changes(
            bead,
            fields=fields,
            add_labels=add_labels,
            remove_labels=remove_labels,
            status=status,
            metadata=metadata,
            force=force,
            actor=self.actor,
        )
        _check(result, f"bd update {bead}")

    def unset_metadata(self, bead: str, keys: Sequence[str]) -> None:
        result = self._routes.issue_unset_metadata(bead, keys, actor=self.actor)
        _check(result, f"bd update --unset-metadata {bead}")

    def close(self, bead: str, reason: str) -> None:
        result = self._routes.issue_close(
            bead, reason=reason, actor=self.actor, force=True, capture=True
        )
        _check(result, f"bd close {bead}")

    def add_dependency(self, bead: str, target: str, type_: str) -> None:
        result = self._routes.dependency_add(bead, target, type_, actor=self.actor, capture=True)
        _check(result, f"bd dep add {bead} {target}")

    def remove_dependency(self, bead: str, target: str) -> None:
        result = self._routes.dependency_remove(bead, target, actor=self.actor, capture=True)
        _check(result, f"bd dep remove {bead} {target}")

    def add_comment(self, bead: str, body: str, author: str | None) -> None:
        result = self._routes.comment_add_authored(
            bead, body, author=author or self.actor, actor=self.actor
        )
        _check(result, f"bd comments add {bead}")

    def delete(self, bead: str) -> None:
        _check(self._routes.issue_delete(bead, actor=self.actor), f"bd delete {bead}")


# -- the loss report -----------------------------------------------------------------------


def batch_digest(events: Iterable[BeadEvent]) -> str:
    """The report's ``archive.manifest_sha256`` when the loader is handed bare events: the
    SHA-256 of the batch's sorted event ids, one per line. A driver that read an archive
    replaces it with that archive's manifest digest (``beadlog.load_archive`` does)."""
    text = "".join(f"{event_id}\n" for event_id in sorted({event.id for event in events}))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class _Report:
    """Accumulates the losses of one load (format ADR section 8)."""

    batch: tuple[BeadEvent, ...]
    losses: list[dict[str, Any]] = field(default_factory=list)
    extra: Counter[str] = field(default_factory=Counter)

    def add(
        self,
        event: BeadEvent,
        code: str,
        disposition: str,
        *,
        member: str = "",
        stored_as: str | None = None,
        detail: str = "",
    ) -> None:
        loss: dict[str, Any] = {
            "event_id": event.id,
            "code": code,
            "member": member,
            "disposition": disposition,
            "stored_as": stored_as,
        }
        if detail:
            loss["detail"] = detail
        self.losses.append(loss)

    def drop(self, event: BeadEvent, code: str, detail: str, *, member: str = "") -> None:
        self.add(event, code, "dropped", member=member, detail=detail)

    def dropped(self) -> set[str]:
        return {loss["event_id"] for loss in self.losses if loss["disposition"] == "dropped"}

    def to_json(self, loader: BdStoreLoader, *, dry_run: bool) -> dict[str, Any]:
        dropped = self.dropped()
        degraded = {loss["event_id"] for loss in self.losses} - dropped
        by_code = Counter(loss["code"] for loss in self.losses) + self.extra
        ordered = sorted(
            self.losses,
            key=lambda loss: (loss["event_id"], loss["code"], loss["member"]),
        )
        return {
            "loss_report_version": LOSS_REPORT_VERSION,
            "archive": {
                "format_version": FORMAT_VERSION,
                "manifest_sha256": batch_digest(self.batch),
            },
            "loader": {
                "name": loader.name,
                "version": loader.version,
                "capabilities": sorted(loader.capabilities),
            },
            "target": loader.target,
            "mode": "dry-run" if dry_run else "apply",
            "summary": {
                "events": len(self.batch),
                "loaded": len(self.batch) - len(dropped),
                "degraded": len(degraded),
                "dropped": len(dropped),
                "by_code": dict(sorted(by_code.items())),
            },
            "losses": ordered,
        }


# -- planning one bead ---------------------------------------------------------------------


def _edge_key(edge: Edge) -> str:
    return f"edge:{edge.type}:{edge.target.group}/{edge.target.id}"


def _deciders(events: Iterable[BeadEvent]) -> dict[str, BeadEvent]:
    """The event deciding each facet under last-writer-wins on the ordering key (the same
    resolution ``beadlog.fold`` applies)."""
    out: dict[str, BeadEvent] = {}
    for event in sorted(events, key=ordering_key):
        kind, data = event.kind, event.data
        if kind is EventKind.CREATE:
            out["exists"] = out["type"] = out["phase"] = event
            for name in data.get("fields") or {}:
                out[f"field:{name}"] = event
        elif kind is EventKind.CHECKPOINT:
            out.setdefault("exists", event)
        elif kind is EventKind.SET:
            out["type" if data["field"] == "type" else f"field:{data['field']}"] = event
        elif kind in (EventKind.LABEL_ADD, EventKind.LABEL_REMOVE):
            out[f"label:{data['label']}"] = event
        elif kind in (EventKind.EDGE_ADD, EventKind.EDGE_REMOVE):
            out[_edge_key(Edge.from_json(data))] = event
        elif kind in (EventKind.PHASE, EventKind.RETIRE):
            out["phase"] = event
        elif kind is EventKind.DELETE:
            out["exists"] = event
        elif kind is EventKind.COMMENT:
            out[f"comment:{event.id}"] = event
    return out


def _status(phase: str | None) -> str:
    return CLOSED if phase == RETIRED else (phase or "open")


def _close_reason(decider: BeadEvent | None) -> str:
    if decider is None:
        return CLOSED
    reason = decider.data.get("reason")
    if isinstance(reason, str) and reason:
        return reason
    return RETIRED if decider.kind is EventKind.RETIRE else CLOSED


def _flag_value(name: str, value: Any) -> str:
    return "" if value is None else str(value)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


@dataclass
class _BeadPlan:
    """The writes one bead needs (empty when it is already converged)."""

    bead: BeadRef
    record: dict[str, Any] | None = None  # a new bead's import record
    fields: dict[str, str] = field(default_factory=dict)
    add_labels: list[str] = field(default_factory=list)
    remove_labels: list[str] = field(default_factory=list)
    status: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    unset: list[str] = field(default_factory=list)
    close: str | None = None
    add_edges: list[Edge] = field(default_factory=list)
    remove_edges: list[Edge] = field(default_factory=list)
    comments: list[tuple[str, str | None]] = field(default_factory=list)
    delete: bool = False
    force: bool = False

    @property
    def update(self) -> bool:
        return bool(
            self.fields or self.add_labels or self.remove_labels or self.status or self.metadata
        )


class BdStoreLoader:
    """BeadLog events into one bd hive (module doc). Also the hive's :class:`StoreReader` and,
    with ``pre_state_path``, its :class:`PreStateExporter` (a ``bd export --all`` file)."""

    name = LOADER_NAME
    version = LOADER_VERSION
    capabilities = CAPABILITIES

    def __init__(
        self,
        port: BdPort,
        *,
        group: str,
        clock: Clock = system_clock,
        pre_state_path: Path | str | None = None,
        delete_enabled: bool = False,
        statuses: Iterable[str] = DEFAULT_STATUSES,
    ) -> None:
        self.port = port
        self.group = group
        self._clock = clock
        self.pre_state_path = Path(pre_state_path) if pre_state_path is not None else None
        self.delete_enabled = delete_enabled
        self.statuses = frozenset(statuses)

    @property
    def target(self) -> str:
        return self.port.target

    # -- reader ports ---------------------------------------------------------------------

    def export(self) -> tuple[BeadEvent, ...]:
        """The store's current history (stored event ids re-emitted), from one bulk read."""
        return baseline_events(
            self.port.export_rows(), group=self.group, extracted_at=self._clock()
        )

    def export_pre_state(self, baseline: Sequence[BeadEvent]) -> str:
        """``bd export --all`` to ``pre_state_path`` (restorable with ``bd import``)."""
        if self.pre_state_path is None:
            raise BdLoadError("no pre-state path configured; refusing to write without one")
        self.pre_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.port.export_to(self.pre_state_path)
        return str(self.pre_state_path)

    # -- loader ---------------------------------------------------------------------------

    def load(self, events: Sequence[BeadEvent], *, dry_run: bool) -> dict[str, Any]:
        """Apply (or with ``dry_run`` only assess) ``events``; return the loss report."""
        batch = tuple(events)
        report = _Report(batch)
        rows = {str(row["id"]): row for row in self.port.export_rows()}
        extracted_at = self._clock()
        accepted = self._screen(batch, rows, report)
        new_ids = {
            bead.id
            for bead, items in accepted.items()
            if bead.id not in rows
            and any(e.kind in (EventKind.CREATE, EventKind.CHECKPOINT) for e in items)
        }
        plans: list[_BeadPlan] = []
        erased = {
            e.bead for e in batch if e.kind is EventKind.TOMBSTONE and e.data.get("scope") == "bead"
        }
        for bead in sorted(erased & set(accepted)):
            for event in accepted.pop(bead):
                report.drop(event, "x-bd.erased", "a bead-scope tombstone erases this bead")
        for bead in sorted(erased):
            if self.delete_enabled and bead.group == self.group and bead.id in rows:
                plans.append(_BeadPlan(bead, delete=True))
        for bead in sorted(accepted):
            plan = self._plan_bead(
                bead, accepted[bead], rows.get(bead.id), rows, new_ids, extracted_at, report
            )
            if plan is not None:
                plans.append(plan)
        if not dry_run:
            self._write(plans)
        return report.to_json(self, dry_run=dry_run)

    def _screen(
        self,
        batch: tuple[BeadEvent, ...],
        rows: Mapping[str, Any],
        report: _Report,
    ) -> dict[BeadRef, list[BeadEvent]]:
        """Drop (and report) what bd cannot take at all; group the rest by bead."""
        accepted: dict[BeadRef, list[BeadEvent]] = {}
        creates = {e.bead for e in batch if e.kind is EventKind.CREATE}
        for event in batch:
            reason = self._refusal(event, rows, creates, report)
            if reason is not None:
                code, detail, member = reason
                report.drop(event, code, detail, member=member)
                continue
            accepted.setdefault(event.bead, []).append(event)
        return accepted

    def _refusal(
        self,
        event: BeadEvent,
        rows: Mapping[str, Any],
        creates: set[BeadRef],
        report: _Report,
    ) -> tuple[str, str, str] | None:
        kind, data = event.kind, event.data
        if event.bead.group != self.group:
            return ("x-bd.foreign-group", f"this hive loads group {self.group}", "/bead/group")
        if event.redacted:
            return ("x-bd.redacted", "a redacted event cannot be stored verifiably", "/redacted")
        if kind is EventKind.DELETE and not self.delete_enabled:
            return ("x-bd.delete-disabled", "bead deletion is disabled for this load", "")
        if kind is EventKind.TOMBSTONE:
            return self._tombstone(event, rows)
        if kind is EventKind.CHECKPOINT and (event.bead.id in rows or event.bead in creates):
            return None  # an assertion over existing history: ignored (format ADR section 9)
        if kind is EventKind.SET:
            problem = self._field_problem(
                data["field"], data["value"], new=event.bead.id not in rows
            )
            if problem:
                return ("field.unsupported", problem, "/data/field")
        if kind is EventKind.CREATE:
            new = event.bead.id not in rows
            for name, value in sorted((data.get("fields") or {}).items()):
                problem = self._field_problem(name, value, new=new)
                if problem:
                    return ("field.unsupported", problem, f"/data/fields/{name}")
        if kind is EventKind.PHASE and data["phase"] not in self.statuses:
            return ("x-bd.unknown-status", f"bd has no status {data['phase']!r}", "/data/phase")
        if kind in (EventKind.EDGE_ADD, EventKind.EDGE_REMOVE):
            if data["target"]["group"] != self.group:
                return (
                    "edge.dangling-target",
                    "bd dependencies cannot name a bead in another group",
                    "/data/target",
                )
        return None

    def _tombstone(self, event: BeadEvent, rows: Mapping[str, Any]) -> tuple[str, str, str]:
        """bd cannot erase: report ``erasure.incomplete`` (and delete a bead when allowed)."""
        if event.data.get("scope") == "bead":
            if self.delete_enabled and event.bead.id in rows:
                detail = "bead deleted; Dolt history still holds its content"
            else:
                detail = "bead deletion is disabled or the bead is absent; nothing was erased"
        else:
            detail = "bd has no verb to remove event content; it survives in bd and Dolt history"
        return ("erasure.incomplete", detail + "; operator action required", "")

    def _field_problem(self, name: str, value: Any, *, new: bool) -> str | None:
        if name.startswith(METADATA_FIELD_PREFIX):
            key = name[len(METADATA_FIELD_PREFIX) :]
            if key == METADATA_KEY or key.startswith(f"{METADATA_KEY}."):
                return f"metadata.{METADATA_KEY} is reserved for the loader's bookkeeping"
            return None
        if name not in UPDATE_FLAGS and name not in IMPORT_ONLY_FIELDS:
            return f"bd has no field {name!r}"
        if name in IMPORT_ONLY_FIELDS and not new:
            return f"bd can set {name!r} only when it creates a bead"
        if value is None:
            if name in UNCLEARABLE_FIELDS:
                return f"bd cannot clear {name!r}"
            return None
        if name in INTEGER_FIELDS:
            if isinstance(value, bool) or not isinstance(value, int):
                return f"bd needs an integer {name!r}"
            return None
        if not isinstance(value, str):
            return f"bd needs a string {name!r}"
        if name in TIMESTAMP_FIELDS and not _TIMESTAMP.fullmatch(value):
            return f"bd needs an RFC 3339 timestamp for {name!r}"
        return None

    def _plan_bead(
        self,
        bead: BeadRef,
        mine: list[BeadEvent],
        row: Mapping[str, Any] | None,
        rows: Mapping[str, Any],
        new_ids: set[str],
        extracted_at: str,
        report: _Report,
    ) -> _BeadPlan | None:
        current = row_state(row, self.group) if row is not None else None
        base = row_events(current, extracted_at) if current is not None else []
        base_ids = {event.id for event in base}
        mine = [
            e
            for e in mine
            if not (e.kind is EventKind.CHECKPOINT and (current is not None or _has_create(mine)))
        ]  # ignored assertions stay loaded and out of the fold
        merged = union_merge([base, mine])  # raises IdCollisionError: the load is refused
        target = fold(merged).get(bead)
        before = fold(base).get(bead) if base else None
        deciders = _deciders(merged)
        deciding = {event.id for event in deciders.values()}
        if target is None or not target.exists:
            deleted = target is not None and target.deleted
            for event in mine:
                if deleted:
                    report.drop(event, "x-bd.deleted", "bd keeps nothing of a deleted bead")
                else:
                    report.drop(event, "x-bd.missing-bead", "the bead has no create to load into")
            if deleted and current is not None:
                return _BeadPlan(bead, delete=True)
            return None
        kept: list[BeadEvent] = []
        for event in mine:
            if event.id in base_ids:
                continue  # already stored: nothing to write, nothing new to report
            if event.id not in deciding:
                report.drop(
                    event,
                    "x-bd.superseded",
                    "a later event of the same facet decides the bead; bd keeps current state only",
                )
                continue
            kept.append(event)
        plan = _BeadPlan(bead)
        stored = dict(current.stored) if current is not None else {}
        document = dict(stored)
        for event in kept:
            key = facet_key(event)
            if key is not None:
                document[key] = envelope(event)
        phase_decider = deciders.get("phase")
        if phase_decider is not None and phase_decider.kind is EventKind.RETIRE:
            document[RETIRED_KEY] = _plain(phase_decider.to_json()["data"])
        else:
            document.pop(RETIRED_KEY, None)

        new = current is None
        for event in kept:
            self._report_kept(event, deciders, new=new, rows=rows, new_ids=new_ids, report=report)
        if new:
            plan.record = self._record(bead, target, deciders, document, report)
            missing = [label for label in self.port.identity_labels if label not in target.labels]
            if missing and kept:
                creator = deciders["exists"]
                report.add(
                    creator,
                    "x-bd.labels-added",
                    "approximated",
                    stored_as="labels",
                    detail="bd import adds the hive identity labels: " + ", ".join(missing),
                )
            return plan
        assert before is not None
        self._diff(plan, before, target, deciders)
        if document != stored:
            plan.metadata[METADATA_KEY] = document
        if not (
            plan.update or plan.unset or plan.close or plan.add_edges or plan.remove_edges
        ) and not any(e.kind is EventKind.COMMENT for e in kept):
            return None
        plan.comments = [
            (str(e.data.get("body", "")), e.actor) for e in kept if e.kind is EventKind.COMMENT
        ]
        return plan

    def _diff(
        self,
        plan: _BeadPlan,
        before: BeadState,
        target: BeadState,
        deciders: Mapping[str, BeadEvent],
    ) -> None:
        old, new = _plain(before.fields), _plain(target.fields)
        if before.type != target.type and target.type:
            plan.fields["type"] = target.type
        for name in sorted(set(old) | set(new)):
            value = new.get(name)
            if old.get(name) == value:
                continue
            if name.startswith(METADATA_FIELD_PREFIX):
                key = name[len(METADATA_FIELD_PREFIX) :]
                if value is None:
                    plan.unset.append(key)
                else:
                    plan.metadata[key] = value
            elif name in UPDATE_FLAGS and not (value is None and name in UNCLEARABLE_FIELDS):
                plan.fields[UPDATE_FLAGS[name]] = _flag_value(name, value)
        plan.force = "assignee" in plan.fields
        plan.add_labels = sorted(set(target.labels) - set(before.labels))
        plan.remove_labels = sorted(set(before.labels) - set(target.labels))
        want, have = _status(target.phase), _status(before.phase)
        if want != have:
            if want == CLOSED:
                plan.close = _close_reason(deciders.get("phase"))
            else:
                plan.status = want
        added = sorted(set(target.edges) - set(before.edges))
        removed = sorted(set(before.edges) - set(target.edges))
        # `bd dep remove` drops every type between a pair: re-add the ones that must stay
        restore = [
            edge
            for edge in target.edges
            if edge not in added and any(gone.target == edge.target for gone in removed)
        ]
        plan.remove_edges = removed
        plan.add_edges = sorted({*added, *restore})

    def _record(
        self,
        bead: BeadRef,
        target: BeadState,
        deciders: Mapping[str, BeadEvent],
        document: Mapping[str, Any],
        report: _Report,
    ) -> dict[str, Any]:
        """The ``bd import`` record of a new bead, carrying the times bd honors."""
        fields = _plain(target.fields)
        creator = deciders["exists"]
        record: dict[str, Any] = {
            "id": bead.id,
            "issue_type": target.type or "task",
            "status": _status(target.phase),
            "labels": list(target.labels),
        }
        if "title" not in fields:
            report.add(
                creator,
                "x-bd.title-required",
                "approximated",
                member="/data/fields/title",
                stored_as=f"title={bead.id}",
                detail="bd requires a title; the bead id stands in",
            )
        if "priority" not in fields:
            report.add(
                creator,
                "x-bd.default-filled",
                "approximated",
                member="/data/fields/priority",
                stored_as="priority=0",
                detail="bd import fills priority 0",
            )
        record["title"] = fields.get("title", bead.id)
        metadata: dict[str, Any] = {}
        for name, value in fields.items():
            if name.startswith(METADATA_FIELD_PREFIX):
                metadata[name[len(METADATA_FIELD_PREFIX) :]] = value
            elif name in FIELD_COLUMNS and name != "title":
                record[FIELD_COLUMNS[name]] = value
        metadata[METADATA_KEY] = dict(document)
        record["metadata"] = metadata
        created = _when(creator)
        if created is not None:
            record["created_at"] = created
        phase = deciders.get("phase")
        if record["status"] == CLOSED:
            record["close_reason"] = _close_reason(phase if phase is not creator else None)
            if phase is not None and phase is not creator and phase.occurred_at is not None:
                record["closed_at"] = phase.occurred_at
        elif record["status"] == "in_progress" and phase is not None and phase is not creator:
            if phase.occurred_at is not None:
                record["started_at"] = phase.occurred_at
        dependencies = []
        for edge in target.edges:
            dep: dict[str, Any] = {
                "issue_id": bead.id,
                "depends_on_id": edge.target.id,
                "type": edge.type,
            }
            decider = deciders.get(_edge_key(edge))
            if decider is not None and decider.occurred_at is not None:
                dep["created_at"] = decider.occurred_at
            dependencies.append(dep)
        if dependencies:
            record["dependencies"] = dependencies
        comments = []
        for comment in target.comments:
            item: dict[str, Any] = {"author": comment.actor or "", "text": comment.body or ""}
            if comment.occurred_at is not None:
                item["created_at"] = comment.occurred_at
            comments.append(item)
        if comments:
            record["comments"] = comments
        return record

    def _report_kept(
        self,
        event: BeadEvent,
        deciders: Mapping[str, BeadEvent],
        *,
        new: bool,
        rows: Mapping[str, Any],
        new_ids: set[str],
        report: _Report,
    ) -> None:
        """The degradations of one stored event (format ADR section 8, bd row)."""
        key = facet_key(event)
        where = f"metadata.{METADATA_KEY}.{key}"
        report.extra["x-bd.non-identity"] += 1  # extracted_at / confidence / evidence / annotations
        if event.occurred_at is None:
            report.add(
                event,
                "valid-time.missing",
                "degraded",
                member="/occurred_at",
                detail="occurred_at is null; bd uses load time",
            )
        elif not (new and _natively_dated(event, deciders)):
            report.add(
                event,
                "valid-time.degraded",
                "degraded",
                member="/occurred_at",
                stored_as=f"{where}.occurred_at",
            )
        if event.occurred_precision is not None:
            report.add(
                event,
                "precision.lost",
                "degraded",
                member="/occurred_precision",
                stored_as=f"{where}.occurred_precision",
            )
        if event.recorded_at is None:
            report.add(
                event,
                "recorded-time.null",
                "reassigned",
                member="/recorded_at",
                detail="bd records the load time",
            )
        else:
            report.add(
                event,
                "recorded-time.reassigned",
                "reassigned",
                member="/recorded_at",
                stored_as=f"{where}.recorded_at",
                detail="bd assigns its own commit time",
            )
        if event.kind is EventKind.RETIRE:
            report.add(
                event,
                "phase.approximated",
                "approximated",
                member="/kind",
                stored_as=f"status=closed; metadata.{METADATA_KEY}.{RETIRED_KEY}",
            )
        if event.kind is EventKind.EDGE_ADD:
            target = event.data["target"]["id"]
            if target not in rows and target not in new_ids:
                report.add(
                    event,
                    "edge.dangling-target",
                    "degraded",
                    member="/data/target",
                    stored_as="dependencies",
                    detail="bd keeps the dependency; its target is not in the store",
                )

    # -- writing --------------------------------------------------------------------------

    def _write(self, plans: Sequence[_BeadPlan]) -> None:
        records = [plan.record for plan in plans if plan.record is not None]
        if records:
            self.port.import_records(records)  # ONE import for every new bead
        for plan in plans:
            if plan.record is not None:
                continue
            bead = plan.bead.id
            if plan.delete:
                self.port.delete(bead)
                continue
            if plan.update:
                self.port.apply_changes(
                    bead,
                    fields=plan.fields,
                    add_labels=plan.add_labels,
                    remove_labels=plan.remove_labels,
                    status=plan.status,
                    metadata=plan.metadata or None,
                    force=plan.force,
                )
            if plan.unset:
                self.port.unset_metadata(bead, plan.unset)
            if plan.close is not None:
                self.port.close(bead, plan.close)
            for edge in plan.remove_edges:
                self.port.remove_dependency(bead, edge.target.id)
            for edge in plan.add_edges:
                self.port.add_dependency(bead, edge.target.id, edge.type)
            for body, author in plan.comments:
                self.port.add_comment(bead, body, author)


def _has_create(events: Iterable[BeadEvent]) -> bool:
    return any(event.kind is EventKind.CREATE for event in events)


def _when(event: BeadEvent) -> str | None:
    if event.kind is EventKind.CHECKPOINT:
        return event.data["covers_through"].get("occurred_at")
    return event.occurred_at


def _natively_dated(event: BeadEvent, deciders: Mapping[str, BeadEvent]) -> bool:
    """Whether a new bead's import carries this event's ``occurred_at`` in a native bd column
    (``created_at``, ``closed_at`` / ``started_at``, a dependency's or comment's
    ``created_at``)."""
    kind = event.kind
    if kind in (EventKind.CREATE, EventKind.CHECKPOINT):
        return deciders.get("exists") is event
    if kind in (EventKind.PHASE, EventKind.RETIRE):
        return deciders.get("phase") is event and _status(event.data.get("phase", RETIRED)) in (
            CLOSED,
            "in_progress",
        )
    if kind is EventKind.EDGE_ADD:
        return True
    return kind is EventKind.COMMENT


# -- composition -----------------------------------------------------------------------


def bd_store_loader(
    cwd: Path | str,
    *,
    group: str,
    actor: str = "",
    pre_state_path: Path | str | None = None,
    delete_enabled: bool = False,
    clock: Clock = system_clock,
) -> BdStoreLoader:
    """The loader for the hive at ``cwd``, writing as :func:`resolve_actor`'s seat identity."""
    port = BdCliPort(cwd, actor=resolve_actor(actor, "", cwd=cwd))
    return BdStoreLoader(
        port,
        group=group,
        clock=clock,
        pre_state_path=pre_state_path,
        delete_enabled=delete_enabled,
    )


def origin_labels(source_kind: str) -> tuple[str, str]:
    """``origin:backfill`` plus ``source:<kind>`` (the open provenance facet) for a pass."""
    if not _SOURCE_KIND.fullmatch(source_kind):
        raise ValueError(f"source kind must match {_SOURCE_KIND.pattern}: {source_kind!r}")
    return (ORIGIN_LABEL, f"source:{source_kind}")


def stamp_labels(events: Iterable[BeadEvent], labels: Iterable[str]) -> tuple[BeadEvent, ...]:
    """``events`` plus a derived ``label_add`` per label for every bead they create, dated with
    the create (so plan, fold and loader agree on the labels a new bead carries). Labels the
    events already add are not duplicated; ids are deterministic."""
    batch = tuple(events)
    wanted = tuple(dict.fromkeys(labels))
    present = {
        (event.bead, event.data["label"]) for event in batch if event.kind is EventKind.LABEL_ADD
    }
    stamped = []
    for create in (event for event in batch if event.kind is EventKind.CREATE):
        for label in wanted:
            if (create.bead, label) in present:
                continue
            present.add((create.bead, label))
            stamped.append(
                mint_event(
                    str(EventKind.LABEL_ADD),
                    create.bead,
                    {"label": label},
                    occurred_at=create.occurred_at,
                    recorded_at=None,
                    extracted_at=create.extracted_at,
                    source=(STAMP_SYSTEM, f"stamp/{create.id}/{label}"),
                    provenance="derived",
                    occurred_precision=None
                    if create.occurred_precision is None
                    else str(create.occurred_precision),
                )
            )
    return (*batch, *stamped)


def default_pre_state_path(plan_path: Path | str) -> Path:
    """Where a pre-state export goes when none is given: beside the plan."""
    path = Path(plan_path)
    return path.with_name(f"{path.stem}.pre-state.jsonl")


__all__ = [
    "CAPABILITIES",
    "DEFAULT_STATUSES",
    "LOADER_NAME",
    "LOADER_VERSION",
    "ORIGIN_LABEL",
    "BdCliPort",
    "BdLoadError",
    "BdPort",
    "BdStoreLoader",
    "bd_store_loader",
    "default_pre_state_path",
    "origin_labels",
    "stamp_labels",
    "system_clock",
]
